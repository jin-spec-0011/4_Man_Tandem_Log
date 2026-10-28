#!/usr/bin/env python3
"""
Personal mail collector
- IMAP 수집: Gmail 2계정 + Naver 1계정 (ACCOUNTS_JSON 시크릿)
- 일별 요약 / 지출(월) / 예약(일자) 추출
- 광고성 대량메일 중 열람률 낮은 발신자 → 차단 후보 등록 (대시보드 승인 후 처리)
- 승인된 발신자: One-Click 수신거부(RFC 8058) + 기존/신규 메일 휴지통 이동
- 결과는 Firebase Realtime DB `pa/` 아래 기록 (본문은 저장하지 않음)

환경변수
  ACCOUNTS_JSON      [{"id":"g1","label":"Gmail 1","host":"imap.gmail.com","user":"a@gmail.com","pass":"앱비밀번호"}, ...]
  FIREBASE_DB_URL    https://<project>-default-rtdb.<region>.firebasedatabase.app
  FIREBASE_SA_JSON   서비스 계정 JSON 원문
  ANTHROPIC_API_KEY  (선택) 지출/예약 정밀 추출
  LLM_MODEL          (선택) 기본 claude-haiku-5-5
  WINDOW_DAYS        (선택) 수집 기간 일수, 기본 rules.json window_days
  DRY_RUN=1          Firebase 대신 collector/out.json 으로 출력
"""
from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import time
import traceback
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr

import requests

KST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
EUC_FAMILY = {"euc-kr", "euc_kr", "ks_c_5601-1987", "ks_c_5601", "ksc5601", "x-windows-949",
              "windows-949", "cp949", "ms949", "uhc"}

# 한국 메일에서 흔한 비표준 charset 이름을 cp949로 매핑
codecs.register(lambda name: codecs.lookup("cp949")
                if name.lower().replace("_", "-") in {"ks-c-5601-1987", "ks-c-5601", "ksc5601",
                                                      "x-windows-949", "windows-949", "ms949", "uhc"}
                else None)

imaplib.Commands.setdefault("MOVE", ("SELECTED",))


def log(*a):
    print(f"[{dt.datetime.now(KST):%H:%M:%S}]", *a, flush=True)


def h16(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


# ─────────────────────────── Storage ───────────────────────────
class Firebase:
    def __init__(self, db_url: str, sa_json: str):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        info = json.loads(sa_json)
        self.creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.database",
                          "https://www.googleapis.com/auth/userinfo.email"])
        self._Request = Request
        self.base = db_url.rstrip("/")
        self.s = requests.Session()

    def _params(self, extra=None):
        if not self.creds.valid:
            self.creds.refresh(self._Request())
        p = {"access_token": self.creds.token}
        if extra:
            p.update(extra)
        return p

    def _url(self, path):
        return f"{self.base}/{path.strip('/')}.json"

    def get(self, path, default=None):
        for attempt in range(3):
            try:
                r = self.s.get(self._url(path), params=self._params(), timeout=30)
                r.raise_for_status()
                v = r.json()
                return default if v is None else v
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def patch(self, path, data: dict):
        items = list(data.items())
        for i in range(0, len(items), 400):
            chunk = dict(items[i:i + 400])
            for attempt in range(3):
                try:
                    r = self.s.patch(self._url(path), params=self._params(),
                                     data=json.dumps(chunk, ensure_ascii=False).encode("utf-8"), timeout=60)
                    r.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))


class DryRunStore:
    def __init__(self, path):
        self.path = path
        self.data = {}

    def get(self, path, default=None):
        return default

    def patch(self, path, data):
        self.data.update(data)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)


# ─────────────────────────── Text helpers ───────────────────────────
def dec_header(v) -> str:
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v)))).strip()
    except Exception:
        return str(v).strip()


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def normalize(s: str) -> str:
    s = s.replace("\r", "").replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def body_text(msg: Message) -> str:
    plain, htmls = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        cs = (part.get_content_charset() or "utf-8").lower()
        if cs in EUC_FAMILY:
            cs = "cp949"
        try:
            txt = payload.decode(cs, errors="replace")
        except LookupError:
            txt = payload.decode("utf-8", errors="replace")
        (plain if ct == "text/plain" else htmls).append(txt)
    joined_plain = "\n".join(plain)
    if len(joined_plain.strip()) > 80:
        return normalize(joined_plain)
    if htmls:
        return normalize(html_to_text("\n".join(htmls)))
    return normalize(joined_plain)


def sender_of(from_header: str) -> tuple[str, str]:
    name, addr = parseaddr(from_header)
    addr = (addr or "").strip().lower()
    name = dec_header(name) or addr.split("@")[0]
    return name.strip('" '), addr


def domain_match(addr: str, domains) -> bool:
    d = addr.split("@")[-1]
    return any(d == x or d.endswith("." + x) for x in domains)


# ─────────────────────────── Classification ───────────────────────────
class Classifier:
    def __init__(self, rules: dict):
        self.r = rules

    def has_any(self, text, words):
        return any(w in text for w in words)

    def is_spend_subject(self, subj):
        return self.has_any(subj, self.r["spend_subject_words"]) and not self.has_any(subj, self.r["promo_words"])

    def is_res_subject(self, subj):
        return self.has_any(subj, self.r["reservation_subject_words"]) and not self.has_any(subj, self.r["promo_words"])

    def is_ad_marked(self, subj):
        head = subj.strip()[:14]
        return any(m in head for m in self.r["ad_subject_markers"])

    def is_bulk(self, subj, addr, list_unsub) -> bool:
        """광고/대량메일 여부. 거래성 제목·보호 발신자는 제외"""
        if self.is_ad_marked(subj):
            return True
        if not list_unsub:
            return False
        if domain_match(addr, self.r["keep_senders"]):
            return False
        if self.is_spend_subject(subj) or self.is_res_subject(subj):
            return False
        return True

    def category(self, *texts) -> str:
        blob = " ".join(t for t in texts if t)
        for cat, words in self.r["categories"].items():
            if any(w.lower() in blob.lower() for w in words):
                return cat
        return "기타"


KRW_RE = re.compile(r"(?:₩|￦|KRW)\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})|([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})\s*원")
USD_RE = re.compile(r"(?:US\$|\$|USD)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
SKIP_LINE_RE = re.compile(r"누적|잔액|한도|포인트|적립|할인|쿠폰|잔여|가용|최소|배송비 무료|이상 구매")
MERCHANT_RE = re.compile(r"(?:이용가맹점|가맹점명|가맹점|사용처|상호명|상호|판매자|Merchant|merchant)\s*[:：]?\s*([^\n]{2,40})")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def find_amount(text: str, labels) -> tuple[float, str] | None:
    lines = text.split("\n")
    # 1) 라벨 기반
    for lab in labels:
        for i, ln in enumerate(lines):
            if lab not in ln or SKIP_LINE_RE.search(ln):
                continue
            seg = ln[ln.index(lab):] + " " + (lines[i + 1] if i + 1 < len(lines) else "")
            m = KRW_RE.search(seg)
            if m:
                v = _num(m.group(1) or m.group(2))
                if v > 0:
                    return v, "KRW"
            m = USD_RE.search(seg)
            if m:
                return _num(m.group(1)), "USD"
    # 2) 첫 번째 원화 금액 (누적/잔액 줄 제외)
    for ln in lines:
        if SKIP_LINE_RE.search(ln):
            continue
        m = KRW_RE.search(ln)
        if m:
            v = _num(m.group(1) or m.group(2))
            if v >= 100:
                return v, "KRW"
    for ln in lines:
        m = USD_RE.search(ln)
        if m and not SKIP_LINE_RE.search(ln):
            return _num(m.group(1)), "USD"
    return None


def parse_spend(subj, body, sender_name, cls: Classifier):
    text = subj + "\n" + body[:6000]
    found = find_amount(text, cls.r["spend_amount_labels"])
    if not found:
        return None
    amount, cur = found
    canceled = cls.has_any(subj, cls.r["cancel_words"])
    m = MERCHANT_RE.search(body[:6000])
    merchant = re.split(r"\s{2,}|\|", m.group(1))[0].strip() if m else sender_name
    merchant = merchant[:40]
    return {
        "amount": -amount if canceled else amount,
        "currency": cur,
        "merchant": merchant,
        "category": cls.category(merchant, subj, sender_name),
        "canceled": canceled,
    }


DATE_PATTERNS = [
    (re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})"), "ymd"),
    (re.compile(r"(?<![\d.])(\d{1,2})\s*월\s*(\d{1,2})\s*일"), "md"),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})"), "mdy_en"),
    (re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+(20\d{2})"), "dmy_en"),
]
TIME_RE = re.compile(r"(?:(오전|오후|AM|PM|am|pm)\s*)?(\d{1,2}):(\d{2})(?:\s*(AM|PM|am|pm))?")
PLACE_RE_TMPL = r"(?:{labels})\s*[:：]?\s*([^\n]{{2,60}})"


def find_dates(text: str, received: dt.date):
    out = []
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "md":
                    mo, da = int(m.group(1)), int(m.group(2))
                    d = dt.date(received.year, mo, da)
                    if d < received - dt.timedelta(days=60):
                        d = dt.date(received.year + 1, mo, da)
                elif kind == "mdy_en":
                    d = dt.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
                else:
                    d = dt.date(int(m.group(3)), MONTHS.index(m.group(2)) + 1, int(m.group(1)))
            except ValueError:
                continue
            out.append((m.start(), m.end(), d))
    out.sort()
    return out


def parse_time_near(text: str, pos: int) -> str | None:
    m = TIME_RE.search(text[pos:pos + 40])
    if not m:
        return None
    hh, mm = int(m.group(2)), int(m.group(3))
    ap = (m.group(1) or m.group(4) or "").lower()
    if ap in ("오후", "pm") and hh < 12:
        hh += 12
    if ap in ("오전", "am") and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def parse_reservation(subj, body, sender_name, received: dt.date, cls: Classifier):
    text = subj + "\n" + body[:8000]
    dates = find_dates(text, received)
    if not dates:
        return None
    future = [x for x in dates if x[2] >= received]
    s, e, d = (future or dates)[0]
    t = parse_time_near(text, e)
    pm = re.search(PLACE_RE_TMPL.format(labels="|".join(map(re.escape, cls.r["place_labels"]))), body[:8000])
    title = re.sub(r"\s+", " ", subj).strip()[:80]
    return {
        "eventDate": d.isoformat(),
        "eventTime": t,
        "place": pm.group(1).strip()[:60] if pm else None,
        "title": title,
        "vendor": sender_name[:40],
        "status": "canceled" if cls.has_any(subj, cls.r["cancel_words"]) else "confirmed",
    }


def llm_refine(kind: str, subj: str, sender: str, received: str, body: str, categories) -> dict | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    if kind == "spend":
        schema = ('{"is_transaction": true|false, "amount": number (원 단위, 취소/환불이면 음수), '
                  '"currency": "KRW"|"USD", "merchant": "가맹점명", "category": "' + "|".join(categories) + '|기타"}')
        task = "다음 메일이 실제 결제/승인/출금 거래 내역인지 판단하고, 거래이면 금액과 가맹점을 추출하라. 광고·쿠폰·할인 안내는 거래가 아니다."
    else:
        schema = ('{"is_reservation": true|false, "title": "짧은 제목", "event_date": "YYYY-MM-DD", '
                  '"event_time": "HH:MM"|null, "place": "장소"|null, "status": "confirmed"|"canceled"}')
        task = "다음 메일이 실제 예약/예매 확정 또는 취소 안내인지 판단하고, 맞다면 이용(방문/탑승/체크인) 일자를 추출하라."
    prompt = (f"{task}\n수신일: {received}\n발신자: {sender}\n제목: {subj}\n본문:\n{body[:3500]}\n\n"
              f"JSON 하나만 출력하라. 형식: {schema}")
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": os.getenv("LLM_MODEL", "claude-haiku-5-5"), "max_tokens": 400,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=45)
        r.raise_for_status()
        txt = "".join(c.get("text", "") for c in r.json().get("content", []))
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:  # LLM 실패 시 규칙 기반 결과 유지
        log(f"  LLM 추출 실패({kind}): {type(e).__name__}")
        return None


# ─────────────────────────── IMAP ───────────────────────────
FETCH_HDR = "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])"
TRASH_FALLBACK = ["[Gmail]/Trash", "[Gmail]/Bin", "Deleted Messages", "Trash", "Deleted Items"]


def imap_date(d: dt.date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def quote_mbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    def __init__(self, acc: dict):
        self.id = acc["id"]
        self.m = imaplib.IMAP4_SSL(acc["host"], int(acc.get("port", 993)), timeout=90)
        self.m.login(acc["user"], acc["pass"])
        typ, data = self.m.capability()
        self.caps = set((data[0] or b"").decode(errors="ignore").upper().split())
        self.trash = self._find_trash()
        typ, _ = self.m.select("INBOX", readonly=False)
        if typ != "OK":
            raise RuntimeError("INBOX 선택 실패")

    def close(self):
        try:
            self.m.close()
        except Exception:
            pass
        try:
            self.m.logout()
        except Exception:
            pass

    def _find_trash(self):
        typ, data = self.m.list()
        if typ != "OK":
            return None
        names = []
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
            names.append(name)
        for fb in TRASH_FALLBACK:
            if fb in names:
                return fb
        return None

    def search_since(self, d: dt.date) -> list[bytes]:
        typ, data = self.m.uid("SEARCH", None, "SINCE", imap_date(d))
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def search_from(self, addr: str) -> list[bytes]:
        safe = addr.replace('"', "")
        typ, data = self.m.uid("SEARCH", None, "FROM", f'"{safe}"')
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch_headers(self, uids: list[bytes]) -> list[dict]:
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
                    rec = self._parse_header_record(meta, hdr)
                    if rec:
                        out.append(rec)
                j += 1
        return out

    @staticmethod
    def _parse_header_record(meta: str, hdr: bytes):
        mu = re.search(r"UID (\d+)", meta)
        if not mu:
            return None
        mf = re.search(r"FLAGS \(([^)]*)\)", meta)
        mi = re.search(r'INTERNALDATE "([^"]+)"', meta)
        flags = (mf.group(1) if mf else "")
        try:
            internal = dt.datetime.strptime(mi.group(1).strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(KST)
        except Exception:
            internal = dt.datetime.now(KST)
        msg = message_from_bytes(hdr)
        name, addr = sender_of(dec_header(msg.get("From")))
        return {
            "uid": mu.group(1).encode(),
            "seen": "\\Seen" in flags,
            "date": internal,
            "from_name": name,
            "from": addr,
            "subject": re.sub(r"\s+", " ", dec_header(msg.get("Subject"))),
            "msgid": (msg.get("Message-ID") or "").strip(),
            "list_unsub": dec_header(msg.get("List-Unsubscribe")),
            "list_unsub_post": dec_header(msg.get("List-Unsubscribe-Post")),
        }

    def fetch_full(self, uid: bytes) -> Message | None:
        typ, data = self.m.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
        if typ != "OK":
            return None
        for item in data or []:
            if isinstance(item, tuple) and item[1]:
                return message_from_bytes(item[1])
        return None

    def move_to_trash(self, uids: list[bytes]) -> int:
        if not uids or not self.trash:
            return 0
        moved = 0
        dest = quote_mbox(self.trash)
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


def unsubscribe(cand: dict) -> dict:
    """RFC 8058 One-Click만 자동 실행. 그 외 링크는 대시보드에서 수동 처리"""
    if cand.get("oneClick") and cand.get("unsubHttp"):
        try:
            r = requests.post(cand["unsubHttp"], data={"List-Unsubscribe": "One-Click"},
                              headers={"User-Agent": "Mozilla/5.0 (personal-mail-collector)"},
                              timeout=20, allow_redirects=True)
            return {"method": "oneclick", "ok": 200 <= r.status_code < 300, "code": r.status_code}
        except requests.RequestException as e:
            return {"method": "oneclick", "ok": False, "code": type(e).__name__}
    if cand.get("unsubHttp"):
        return {"method": "manual_link", "ok": False}
    if cand.get("unsubMailto"):
        return {"method": "manual_mailto", "ok": False}
    return {"method": "none", "ok": False}


def unsub_fields(list_unsub: str, list_unsub_post: str) -> dict:
    urls = re.findall(r"<([^>]+)>", list_unsub or "")
    https = next((u.strip() for u in urls if u.strip().lower().startswith("https://")), None)
    mailto = next((u.strip() for u in urls if u.strip().lower().startswith("mailto:")), None)
    return {"unsubHttp": https, "unsubMailto": mailto,
            "oneClick": bool(https and "one-click" in (list_unsub_post or "").lower())}


# ─────────────────────────── Per-account pipeline ───────────────────────────
def process_account(acc, rules, cls: Classifier, store, ctx, updates: dict) -> dict:
    aid = acc["id"]
    stat = {"ok": True, "trashedBlocked": 0, "unsubscribed": 0, "scanned": 0, "spend": 0, "reservations": 0}
    now = ctx["now"]
    mb = Mailbox(acc)
    try:
        if not mb.trash:
            log(f"  [{aid}] 휴지통 폴더 미탐지 → 이동 작업 생략")
        # 1) 승인된 차단 후보 처리
        for key, c in list(ctx["candidates"].items()):
            if c.get("account") != aid or c.get("status") != "approved":
                continue
            res = unsubscribe(c)
            moved = mb.move_to_trash(mb.search_from(c["sender"])) if rules.get("auto_trash_blocked", True) else 0
            updates[f"blockCandidates/{key}/status"] = "done"
            updates[f"blockCandidates/{key}/result"] = {**res, "trashed": moved, "at": now.isoformat()}
            updates[f"blocklist/{key}"] = {"sender": c["sender"], "name": c.get("name", ""), "account": aid,
                                           "since": now.date().isoformat()}
            ctx["blocklist"][key] = {"sender": c["sender"], "account": aid}
            stat["unsubscribed"] += 1 if res.get("ok") else 0
            stat["trashedBlocked"] += moved
            log(f"  [{aid}] 차단 처리 {c['sender']} → {res['method']} / 휴지통 {moved}건")

        # 2) 차단 목록 발신자 신규 메일 휴지통 이동
        if rules.get("auto_trash_blocked", True):
            for key, b in ctx["blocklist"].items():
                if b.get("account") == aid and b.get("sender"):
                    stat["trashedBlocked"] += mb.move_to_trash(mb.search_from(b["sender"]))

        # 3) 헤더 스캔
        win_days = ctx["window_days"]
        scan_days = max(int(rules["interest_scan_days"]), win_days)
        since_scan = (now - dt.timedelta(days=scan_days)).date()
        win_start = (now - dt.timedelta(days=win_days - 1)).date()
        headers = mb.fetch_headers(mb.search_since(since_scan))
        stat["scanned"] = len(headers)

        # 4) 관심도 분석 → 차단 후보
        senders: dict[str, dict] = {}
        for h in headers:
            h["bulk"] = cls.is_bulk(h["subject"], h["from"], h["list_unsub"])
            if not h["bulk"] or not h["from"]:
                continue
            s = senders.setdefault(h["from"], {"name": h["from_name"], "total": 0, "read": 0,
                                                "last": h["date"], "subject": h["subject"],
                                                "lu": h["list_unsub"], "lup": h["list_unsub_post"]})
            s["total"] += 1
            s["read"] += 1 if h["seen"] else 0
            if h["date"] >= s["last"]:
                s.update(last=h["date"], subject=h["subject"], name=h["from_name"])
                if h["list_unsub"]:
                    s.update(lu=h["list_unsub"], lup=h["list_unsub_post"])
        br = rules["block_rule"]
        for addr, s in senders.items():
            key = h16(f"{aid}|{addr}")
            if key in ctx["blocklist"] or key in ctx["allowlist"]:
                continue
            existing = ctx["candidates"].get(key)
            if existing and existing.get("status") in ("approved", "done", "rejected"):
                continue
            rate = s["read"] / s["total"]
            qualifies = s["total"] >= br["min_ad_mails"] and rate <= br["max_read_rate"]
            if qualifies:
                updates[f"blockCandidates/{key}"] = {
                    "account": aid, "sender": addr, "name": s["name"][:60],
                    "total": s["total"], "read": s["read"], "rate": round(rate, 3),
                    "lastSubject": s["subject"][:100], "lastAt": s["last"].isoformat(),
                    "status": "pending", "updatedAt": now.isoformat(),
                    **unsub_fields(s["lu"], s["lup"]),
                }
            elif existing and existing.get("status") == "pending":
                updates[f"blockCandidates/{key}"] = None  # 다시 읽기 시작 → 후보 해제

        # 5) 수집 윈도우: 일별 요약 + 지출/예약 추출
        daily: dict[str, dict] = {}
        for i in range(win_days):
            d = (win_start + dt.timedelta(days=i)).isoformat()
            daily[d] = {"total": 0, "unread": 0, "bulk": 0, "spend": 0, "reservations": 0, "important": []}
        for h in headers:
            dkey = h["date"].date().isoformat()
            if dkey not in daily:
                continue
            day = daily[dkey]
            day["total"] += 1
            day["unread"] += 0 if h["seen"] else 1
            day["bulk"] += 1 if h["bulk"] else 0
            if not h["bulk"] and not h["seen"]:
                day["important"].append({"from": h["from_name"][:40], "subject": h["subject"][:100],
                                         "time": h["date"].strftime("%H:%M")})
            if h["bulk"] or cls.is_ad_marked(h["subject"]):
                continue
            is_sp, is_rs = cls.is_spend_subject(h["subject"]), cls.is_res_subject(h["subject"])
            if not (is_sp or is_rs):
                continue
            mkey = h16(h["msgid"] or f"{aid}|{h['uid'].decode()}|{h['subject']}")
            month = h["date"].strftime("%Y-%m")
            if is_sp and mkey in ctx["spend_month"](month):
                is_sp = False
            if is_rs and mkey in ctx["reservations"]:
                is_rs = False
            if not (is_sp or is_rs):
                continue
            full = mb.fetch_full(h["uid"])
            if full is None:
                continue
            body = body_text(full)
            if is_sp:
                sp = parse_spend(h["subject"], body, h["from_name"], cls)
                ref = llm_refine("spend", h["subject"], h["from_name"], dkey, body, list(rules["categories"]))
                if ref is not None:
                    if not ref.get("is_transaction"):
                        sp = None
                    else:
                        sp = sp or {}
                        try:
                            sp["amount"] = float(ref.get("amount", sp.get("amount", 0)))
                        except (TypeError, ValueError):
                            pass
                        sp["currency"] = ref.get("currency") or sp.get("currency", "KRW")
                        sp["merchant"] = (ref.get("merchant") or sp.get("merchant") or h["from_name"])[:40]
                        cat = ref.get("category")
                        sp["category"] = cat if cat in rules["categories"] or cat == "기타" else sp.get("category", "기타")
                        sp["canceled"] = sp["amount"] < 0
                if sp and sp.get("amount"):
                    updates[f"spend/{month}/{mkey}"] = {**sp, "date": dkey, "time": h["date"].strftime("%H:%M"),
                                                       "account": aid, "subject": h["subject"][:100],
                                                       "from": h["from_name"][:40]}
                    ctx["spend_month"](month)[mkey] = True
                    if sp.get("currency", "KRW") == "KRW":
                        day["spend"] += sp["amount"]
                    stat["spend"] += 1
                    continue
            if is_rs:
                rs = parse_reservation(h["subject"], body, h["from_name"], h["date"].date(), cls)
                ref = llm_refine("reservation", h["subject"], h["from_name"], dkey, body, [])
                if ref is not None:
                    if not ref.get("is_reservation"):
                        rs = None
                    else:
                        rs = rs or {"vendor": h["from_name"][:40]}
                        if ref.get("event_date") and re.match(r"^\d{4}-\d{2}-\d{2}$", str(ref["event_date"])):
                            rs["eventDate"] = ref["event_date"]
                        rs["eventTime"] = ref.get("event_time") or rs.get("eventTime")
                        rs["place"] = ref.get("place") or rs.get("place")
                        rs["title"] = (ref.get("title") or rs.get("title") or h["subject"])[:80]
                        rs["status"] = ref.get("status") if ref.get("status") in ("confirmed", "canceled") else rs.get("status", "confirmed")
                if rs and rs.get("eventDate"):
                    rec = {**rs, "account": aid, "receivedAt": h["date"].isoformat(), "subject": h["subject"][:100]}
                    if rs["status"] == "canceled":
                        # 같은 업체·같은 일자의 기존 확정 예약을 취소 처리
                        for rk, rv in ctx["reservations"].items():
                            if isinstance(rv, dict) and rv.get("vendor") == rs["vendor"] and rv.get("eventDate") == rs["eventDate"]:
                                rv["status"] = "canceled"
                                full_path = f"reservations/{rk}"
                                if full_path in updates:  # 같은 PATCH 안에서 상위/하위 경로 충돌 방지
                                    updates[full_path]["status"] = "canceled"
                                else:
                                    updates[f"{full_path}/status"] = "canceled"
                    updates[f"reservations/{mkey}"] = rec
                    ctx["reservations"][mkey] = rec
                    day["reservations"] += 1
                    stat["reservations"] += 1
        for dkey, day in daily.items():
            day["important"].sort(key=lambda x: x["time"], reverse=True)
            day["important"] = day["important"][: int(rules.get("important_limit", 15))]
            day["spend"] = round(day["spend"])
            updates[f"daily/{dkey}/{aid}"] = day
    finally:
        mb.close()
    return stat


def main() -> int:
    with open(os.path.join(HERE, "rules.json"), encoding="utf-8") as f:
        rules = json.load(f)
    try:
        accounts = json.loads(os.environ["ACCOUNTS_JSON"])
        assert isinstance(accounts, list) and accounts
        for a in accounts:
            for k in ("id", "host", "user", "pass"):
                assert a.get(k), f"계정 항목에 '{k}' 누락"
            assert re.match(r"^[A-Za-z0-9_-]{1,20}$", a["id"]), f"id 형식 오류: {a['id']}"
    except (KeyError, json.JSONDecodeError, AssertionError) as e:
        log(f"ACCOUNTS_JSON 오류: {e}")
        return 2

    if os.getenv("DRY_RUN") == "1":
        store = DryRunStore(os.path.join(HERE, "out.json"))
    else:
        if not os.getenv("FIREBASE_DB_URL") or not os.getenv("FIREBASE_SA_JSON"):
            log("FIREBASE_DB_URL / FIREBASE_SA_JSON 미설정")
            return 2
        store = Firebase(os.environ["FIREBASE_DB_URL"], os.environ["FIREBASE_SA_JSON"])

    now = dt.datetime.now(KST)
    win = int(os.getenv("WINDOW_DAYS") or rules["window_days"])
    win = max(1, min(win, 120))
    spend_cache: dict[str, dict] = {}

    def spend_month(m):
        if m not in spend_cache:
            spend_cache[m] = dict.fromkeys((store.get(f"pa/spend/{m}", {}) or {}).keys(), True)
        return spend_cache[m]

    ctx = {
        "now": now,
        "window_days": win,
        "candidates": store.get("pa/blockCandidates", {}) or {},
        "blocklist": store.get("pa/blocklist", {}) or {},
        "allowlist": store.get("pa/allowlist", {}) or {},
        "reservations": store.get("pa/reservations", {}) or {},
        "spend_month": spend_month,
    }
    cls = Classifier(rules)
    run_log = {"startedAt": now.isoformat(), "windowDays": win, "accounts": {}}
    failed = 0
    for acc in accounts:
        log(f"계정 처리 시작: {acc['id']}")
        updates: dict = {f"accounts/{acc['id']}": {"label": acc.get("label", acc["id"])}}
        try:
            st = process_account(acc, rules, cls, store, ctx, updates)
            run_log["accounts"][acc["id"]] = st
            log(f"  [{acc['id']}] 완료: {st}")
        except imaplib.IMAP4.error as e:
            failed += 1
            run_log["accounts"][acc["id"]] = {"ok": False, "error": f"IMAP 인증/명령 오류: {str(e)[:200]}"}
            log(f"  [{acc['id']}] IMAP 오류: {e}")
        except Exception as e:
            failed += 1
            run_log["accounts"][acc["id"]] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            log(f"  [{acc['id']}] 실패:\n{traceback.format_exc()}")
        if updates:
            store.patch("pa", updates)
    run_log["finishedAt"] = dt.datetime.now(KST).isoformat()
    run_log["ok"] = failed == 0
    store.patch("pa", {"meta/lastRun": run_log})
    log(f"종료: 실패 {failed}/{len(accounts)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
