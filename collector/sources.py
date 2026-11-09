"""메일 소스 어댑터.

공통 인터페이스
  headers_since(date) -> list[Header dict]
  body(mid) -> str                    본문 텍스트 (읽음 상태 변경 없음)
  trash_from(sender, limit) -> int    해당 발신자 메일을 휴지통으로 이동 (영구 삭제 아님)
  send_self(subject, text) -> bool    본인에게 알림 메일 (Gmail 전용, IMAP 은 False)
  close()

Header dict 키: id, seen, date(KST datetime), from_name, from, subject, msgid,
               list_unsub, list_unsub_post, promo(bool)
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import datetime as dt
import imaplib
import re
import threading
import time
from email import message_from_bytes
from email.mime.text import MIMEText

import requests

from parse import KST, MONTHS, dec_header, pick_body, safe_charset, sender_of, body_text

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
META_HEADERS = ["From", "Subject", "Message-ID", "List-Unsubscribe", "List-Unsubscribe-Post"]


class SourceAuthError(RuntimeError):
    """자격 증명 오류 (refresh token 만료/폐기, 앱 비밀번호 오류 등)"""


# ─────────────────────────── Gmail API ───────────────────────────
class GmailSource:
    kind = "gmail"

    def __init__(self, acc_id: str, client_id: str, client_secret: str, refresh_token: str, workers: int = 6):
        self.id = acc_id
        self._cid, self._csec, self._rt = client_id, client_secret, refresh_token
        self._token, self._exp = None, 0.0
        self._lock = threading.Lock()
        self._local = threading.local()
        self.workers = max(1, min(workers, 10))
        self._refresh()
        prof = self._get("profile")
        self.email = prof.get("emailAddress", "")

    # --- auth ---
    def _refresh(self):
        try:
            r = requests.post(TOKEN_URL, data={"client_id": self._cid, "client_secret": self._csec,
                                               "refresh_token": self._rt, "grant_type": "refresh_token"},
                              timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(f"토큰 엔드포인트 연결 실패: {type(e).__name__}") from e
        if r.status_code != 200:
            try:
                err = r.json()
            except ValueError:
                err = {"error": r.text[:200]}
            if err.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise SourceAuthError(
                    f"Gmail OAuth 오류({err.get('error')}): refresh token 재발급 필요 "
                    f"(동의 화면 '테스트' 상태면 7일 후 만료됨 → '프로덕션'으로 게시)")
            raise RuntimeError(f"토큰 갱신 실패 {r.status_code}: {err}")
        j = r.json()
        self._token = j["access_token"]
        self._exp = time.time() + int(j.get("expires_in", 3600)) - 120

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def _call(self, method: str, path: str, params=None, json_body=None):
        last = None
        for attempt in range(5):
            with self._lock:
                if time.time() >= self._exp:
                    self._refresh()
                token = self._token
            try:
                r = self._session().request(method, f"{GMAIL_API}/{path}", params=params, json=json_body,
                                            headers={"Authorization": f"Bearer {token}"}, timeout=45)
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                with self._lock:
                    self._exp = 0
                last = RuntimeError("401")
                continue
            if r.status_code in (429, 500, 502, 503, 504) or (
                    r.status_code == 403 and "rateLimitExceeded" in r.text):
                last = RuntimeError(f"{r.status_code}")
                time.sleep(min(30, 2 ** attempt + 0.5))
                continue
            if r.status_code == 403:
                raise SourceAuthError(f"Gmail 권한 부족(403): gmail.modify 범위로 refresh token 재발급 필요 — {r.text[:160]}")
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"Gmail API {r.status_code}: {r.text[:200]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"Gmail API 재시도 초과 {path}: {last}")

    def _get(self, path, params=None):
        return self._call("GET", path, params=params) or {}

    # --- list ---
    def _list_ids(self, q: str, cap: int = 5000) -> list[str]:
        ids, token = [], None
        while len(ids) < cap:
            p = {"q": q, "maxResults": 500, "includeSpamTrash": "false"}
            if token:
                p["pageToken"] = token
            j = self._get("messages", p)
            ids += [m["id"] for m in j.get("messages", [])]
            token = j.get("nextPageToken")
            if not token:
                break
        return ids[:cap]

    def _meta(self, mid: str) -> dict | None:
        j = self._call("GET", f"messages/{mid}",
                       params=[("format", "metadata")] + [("metadataHeaders", h) for h in META_HEADERS])
        if not j:
            return None
        hdr = {h["name"].lower(): h.get("value", "") for h in j.get("payload", {}).get("headers", [])}
        labels = set(j.get("labelIds", []))
        try:
            date = dt.datetime.fromtimestamp(int(j.get("internalDate", "0")) / 1000, KST)
        except (TypeError, ValueError):
            date = dt.datetime.now(KST)
        name, addr = sender_of(dec_header(hdr.get("from")))
        return {
            "id": mid,
            "seen": "UNREAD" not in labels,
            "date": date,
            "from_name": name,
            "from": addr,
            "subject": re.sub(r"\s+", " ", dec_header(hdr.get("subject"))),
            "msgid": (hdr.get("message-id") or "").strip(),
            "list_unsub": dec_header(hdr.get("list-unsubscribe")),
            "list_unsub_post": dec_header(hdr.get("list-unsubscribe-post")),
            "promo": "CATEGORY_PROMOTIONS" in labels,
        }

    def headers_since(self, since: dt.date) -> list[dict]:
        # after: 는 epoch 초 지원 → KST 자정 기준 정확한 경계
        start = int(dt.datetime.combine(since, dt.time(0, 0), KST).timestamp())
        ids = self._list_ids(f"after:{start} -in:chats")
        out: list[dict] = []
        with cf.ThreadPoolExecutor(self.workers) as ex:
            for rec in ex.map(self._safe_meta, ids):
                if rec:
                    out.append(rec)
        return out

    def _safe_meta(self, mid):
        try:
            return self._meta(mid)
        except SourceAuthError:
            raise
        except Exception:
            return None

    # --- body ---
    def body(self, mid: str) -> str:
        j = self._call("GET", f"messages/{mid}", params={"format": "full"})
        if not j:
            return ""
        plain, htmls = [], []

        def walk(part):
            mt = part.get("mimeType", "")
            if part.get("filename"):
                return
            if mt in ("text/plain", "text/html"):
                data = part.get("body", {}).get("data")
                if data:
                    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                    cs = None
                    for h in part.get("headers", []):
                        if h.get("name", "").lower() == "content-type":
                            m = re.search(r'charset="?([^";\s]+)', h.get("value", ""), re.I)
                            cs = m.group(1) if m else None
                    txt = raw.decode(safe_charset(cs), errors="replace")
                    (plain if mt == "text/plain" else htmls).append(txt)
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(j.get("payload", {}))
        return pick_body(plain, htmls)

    # --- actions ---
    def trash_from(self, sender: str, limit: int = 500) -> int:
        safe = re.sub(r'[\s"()]', "", sender)
        if not safe or "@" not in safe:
            return 0
        n = 0
        for mid in self._list_ids(f"from:{safe}", cap=limit):
            try:
                if self._call("POST", f"messages/{mid}/trash") is not None:
                    n += 1
            except SourceAuthError:
                raise
            except Exception:
                continue
        return n

    def send_self(self, subject: str, text: str) -> bool:
        if not self.email:
            return False
        msg = MIMEText(text, "plain", "utf-8")
        msg["To"] = self.email
        msg["From"] = self.email
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self._call("POST", "messages/send", json_body={"raw": raw}) is not None

    def close(self):
        pass


# ─────────────────────────── IMAP (Naver) ───────────────────────────
imaplib.Commands.setdefault("MOVE", ("SELECTED",))
FETCH_HDR = ("(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS "
             "(FROM SUBJECT MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])")
TRASH_FALLBACK = ["휴지통", "Deleted Messages", "Trash", "Deleted Items", "[Gmail]/Trash"]


def imap_date(d: dt.date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def _imap_utf7_decode(s: str) -> str:
    """IMAP modified UTF-7 폴더명 디코드 (네이버 '휴지통' 등 한글 폴더)."""
    def dec(m):
        b = m.group(1)
        if b == "":
            return "&"
        b = b.replace(",", "/")
        b += "=" * (-len(b) % 4)
        return base64.b64decode(b).decode("utf-16-be", errors="replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, s)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapSource:
    kind = "imap"

    def __init__(self, acc_id: str, host: str, user: str, password: str, port: int = 993):
        self.id = acc_id
        self.email = user
        try:
            self.m = imaplib.IMAP4_SSL(host, port, timeout=90)
            self.m.login(user, password)
        except imaplib.IMAP4.error as e:
            raise SourceAuthError(f"IMAP 로그인 실패: 아이디/앱 비밀번호 및 IMAP 사용 설정 확인 ({str(e)[:120]})") from e
        typ, data = self.m.capability()
        self.caps = set((data[0] or b"").decode(errors="ignore").upper().split())
        self.trash = self._find_trash()
        typ, _ = self.m.select("INBOX", readonly=False)
        if typ != "OK":
            raise RuntimeError("INBOX 선택 실패")

    def close(self):
        for fn in (self.m.close, self.m.logout):
            try:
                fn()
            except Exception:
                pass

    def _find_trash(self) -> str | None:
        typ, data = self.m.list()
        if typ != "OK":
            return None
        names = {}
        for raw in data or []:
            if not isinstance(raw, bytes):
                continue
            line = raw.decode(errors="ignore")
            mm = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if not mm:
                continue
            name = mm.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if "\\trash" in mm.group("flags").lower():
                return name
            names[_imap_utf7_decode(name)] = name
        for fb in TRASH_FALLBACK:
            if fb in names:
                return names[fb]
        return None

    def _search(self, *crit) -> list[bytes]:
        typ, data = self.m.uid("SEARCH", None, *crit)
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def headers_since(self, since: dt.date) -> list[dict]:
        uids = self._search("SINCE", imap_date(since))
        out = []
        for i in range(0, len(uids), 250):
            chunk = b",".join(uids[i:i + 250]).decode()
            typ, data = self.m.uid("FETCH", chunk, FETCH_HDR)
            if typ != "OK":
                continue
            j = 0
            while j < len(data):
                item = data[j]
                if isinstance(item, tuple):
                    meta = item[0].decode(errors="ignore")
                    hdr = item[1] or b""
                    if j + 1 < len(data) and isinstance(data[j + 1], bytes):
                        meta += " " + data[j + 1].decode(errors="ignore")
                        j += 1
                    rec = self._parse(meta, hdr)
                    if rec and rec["date"].date() >= since:
                        out.append(rec)
                j += 1
        return out

    @staticmethod
    def _parse(meta: str, hdr: bytes) -> dict | None:
        mu = re.search(r"UID (\d+)", meta)
        if not mu:
            return None
        mf = re.search(r"FLAGS \(([^)]*)\)", meta)
        mi = re.search(r'INTERNALDATE "([^"]+)"', meta)
        try:
            internal = dt.datetime.strptime(mi.group(1).strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(KST)
        except Exception:
            internal = dt.datetime.now(KST)
        msg = message_from_bytes(hdr)
        name, addr = sender_of(dec_header(msg.get("From")))
        return {
            "id": mu.group(1),
            "seen": "\\Seen" in (mf.group(1) if mf else ""),
            "date": internal,
            "from_name": name,
            "from": addr,
            "subject": re.sub(r"\s+", " ", dec_header(msg.get("Subject"))),
            "msgid": (msg.get("Message-ID") or "").strip(),
            "list_unsub": dec_header(msg.get("List-Unsubscribe")),
            "list_unsub_post": dec_header(msg.get("List-Unsubscribe-Post")),
            "promo": False,
        }

    def body(self, mid: str) -> str:
        typ, data = self.m.uid("FETCH", str(mid), "(BODY.PEEK[])")
        if typ != "OK":
            return ""
        for item in data or []:
            if isinstance(item, tuple) and item[1]:
                return body_text(message_from_bytes(item[1]))
        return ""

    def trash_from(self, sender: str, limit: int = 500) -> int:
        if not self.trash:
            return 0
        safe = re.sub(r'[\s"()\\]', "", sender)
        if not safe or "@" not in safe:
            return 0
        uids = self._search("FROM", f'"{safe}"')[:limit]
        moved = 0
        dest = _quote(self.trash)
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200]).decode()
            if "MOVE" in self.caps:
                typ, _ = self.m.uid("MOVE", chunk, dest)
            else:
                typ, _ = self.m.uid("COPY", chunk, dest)
                if typ == "OK":
                    self.m.uid("STORE", chunk, "+FLAGS.SILENT", "(\\Deleted)")
                    if "UIDPLUS" in self.caps:
                        self.m.uid("EXPUNGE", chunk)
                    else:
                        self.m.expunge()
            if typ == "OK":
                moved += len(uids[i:i + 200])
        return moved

    def send_self(self, subject: str, text: str) -> bool:
        return False


def unsubscribe_oneclick(url: str) -> dict:
    """RFC 8058 One-Click 수신거부. https 만 허용."""
    if not url or not url.lower().startswith("https://"):
        return {"method": "none", "ok": False}
    try:
        r = requests.post(url, data={"List-Unsubscribe": "One-Click"},
                          headers={"User-Agent": "Mozilla/5.0 (personal-mail-dashboard)"},
                          timeout=20, allow_redirects=True)
        return {"method": "oneclick", "ok": 200 <= r.status_code < 300, "code": r.status_code}
    except requests.RequestException as e:
        return {"method": "oneclick", "ok": False, "code": type(e).__name__}


__all__ = ["GmailSource", "ImapSource", "SourceAuthError", "unsubscribe_oneclick"]
