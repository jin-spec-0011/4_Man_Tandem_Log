"""메일 텍스트 처리 · 분류 · 지출/예약 추출 (네트워크 의존성 없음 → 단위 테스트 대상)."""
from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import html
import re
import unicodedata
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr

KST = dt.timezone(dt.timedelta(hours=9))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_CP949_ALIASES = {"ks-c-5601-1987", "ks-c-5601", "ksc5601", "x-windows-949", "windows-949", "ms949", "uhc",
                  "euc-kr", "cp949"}

# 한국 메일에서 흔한 비표준 charset 이름 → cp949
codecs.register(lambda name: codecs.lookup("cp949")
                if name.lower().replace("_", "-") in _CP949_ALIASES - {"cp949", "euc-kr"} else None)


def h16(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


def safe_charset(cs: str | None) -> str:
    cs = (cs or "utf-8").lower().replace("_", "-")
    if cs in _CP949_ALIASES:
        return "cp949"
    try:
        codecs.lookup(cs)
        return cs
    except LookupError:
        return "utf-8"


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


def pick_body(plain: list[str], htmls: list[str]) -> str:
    """text/plain 이 충분하면 우선, 아니면 HTML → 텍스트."""
    joined = "\n".join(plain)
    if len(joined.strip()) > 80:
        return normalize(joined)
    if htmls:
        return normalize(html_to_text("\n".join(htmls)))
    return normalize(joined)


def body_text(msg: Message) -> str:
    plain, htmls = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        txt = payload.decode(safe_charset(part.get_content_charset()), errors="replace")
        (plain if ct == "text/plain" else htmls).append(txt)
    return pick_body(plain, htmls)


def sender_of(from_header: str) -> tuple[str, str]:
    name, addr = parseaddr(from_header or "")
    addr = (addr or "").strip().lower()
    name = dec_header(name) or addr.split("@")[0]
    return name.strip('" '), addr


def domain_match(addr: str, domains) -> bool:
    d = addr.split("@")[-1]
    return any(d == x or d.endswith("." + x) for x in domains)


def unsub_fields(list_unsub: str, list_unsub_post: str) -> dict:
    urls = re.findall(r"<([^>]+)>", list_unsub or "")
    https = next((u.strip() for u in urls if u.strip().lower().startswith("https://")), None)
    mailto = next((u.strip() for u in urls if u.strip().lower().startswith("mailto:")), None)
    return {"unsubHttp": https, "unsubMailto": mailto,
            "oneClick": bool(https and "one-click" in (list_unsub_post or "").lower())}


# ─────────────────────────── Classification ───────────────────────────
class Classifier:
    def __init__(self, rules: dict):
        self.r = rules

    @staticmethod
    def has_any(text: str, words) -> bool:
        return any(w in text for w in words)

    def is_spend_subject(self, subj: str) -> bool:
        return self.has_any(subj, self.r["spend_subject_words"]) and not self.has_any(subj, self.r["promo_words"])

    def is_res_subject(self, subj: str) -> bool:
        return self.has_any(subj, self.r["reservation_subject_words"]) and not self.has_any(subj, self.r["promo_words"])

    def is_ad_marked(self, subj: str) -> bool:
        head = subj.strip()[:14]
        return any(m in head for m in self.r["ad_subject_markers"])

    def is_ad(self, subj: str, addr: str, list_unsub: str, promo_label: bool = False) -> bool:
        """광고/대량메일 여부. 거래성 제목·보호 발신자는 제외.
        promo_label: Gmail CATEGORY_PROMOTIONS 라벨 보유 여부"""
        if self.is_ad_marked(subj):
            return True
        if domain_match(addr, self.r["keep_senders"]):
            return False
        if self.is_spend_subject(subj) or self.is_res_subject(subj):
            return False
        return bool(list_unsub) or promo_label

    def category(self, *texts) -> str:
        blob = " ".join(t for t in texts if t).lower()
        for cat, words in self.r["categories"].items():
            if any(w.lower() in blob for w in words):
                return cat
        return "기타"


# ─────────────────────────── Spend ───────────────────────────
KRW_RE = re.compile(r"(?:₩|￦|KRW)\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})|([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})\s*원")
USD_RE = re.compile(r"(?:US\$|\$|USD)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
JPY_RE = re.compile(r"(?:¥|￥|JPY)\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})|([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})\s*円")
SKIP_LINE_RE = re.compile(r"누적|잔액|한도|포인트|적립|할인|쿠폰|잔여|가용|최소|배송비 무료|이상 구매")
MERCHANT_RE = re.compile(r"(?:이용가맹점|가맹점명|가맹점|사용처|상호명|상호|판매자|Merchant|merchant)\s*[:：]?\s*([^\n]{2,40})")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _match_amount(seg: str) -> tuple[float, str] | None:
    m = KRW_RE.search(seg)
    if m:
        v = _num(m.group(1) or m.group(2))
        if v > 0:
            return v, "KRW"
    m = JPY_RE.search(seg)
    if m:
        v = _num(m.group(1) or m.group(2))
        if v > 0:
            return v, "JPY"
    m = USD_RE.search(seg)
    if m:
        v = _num(m.group(1))
        if v > 0:
            return v, "USD"
    return None


def find_amount(text: str, labels) -> tuple[float, str] | None:
    lines = text.split("\n")
    # 1) 라벨 기반 (같은 줄 + 다음 줄)
    for lab in labels:
        for i, ln in enumerate(lines):
            if lab not in ln or SKIP_LINE_RE.search(ln):
                continue
            seg = ln[ln.index(lab):] + " " + (lines[i + 1] if i + 1 < len(lines) else "")
            hit = _match_amount(seg)
            if hit:
                return hit
    # 2) 첫 번째 원화 금액 (누적/잔액 줄 제외, 100원 미만 무시)
    for ln in lines:
        if SKIP_LINE_RE.search(ln):
            continue
        m = KRW_RE.search(ln)
        if m:
            v = _num(m.group(1) or m.group(2))
            if v >= 100:
                return v, "KRW"
    # 3) 외화
    for ln in lines:
        if SKIP_LINE_RE.search(ln):
            continue
        hit = _match_amount(ln)
        if hit and hit[1] != "KRW":
            return hit
    return None


def parse_spend(subj: str, body: str, sender_name: str, cls: Classifier) -> dict | None:
    text = unicodedata.normalize("NFKC", subj + "\n" + body[:6000])
    found = find_amount(text, cls.r["spend_amount_labels"])
    if not found:
        return None
    amount, cur = found
    canceled = cls.has_any(subj, cls.r["cancel_words"])
    m = MERCHANT_RE.search(body[:6000])
    merchant = re.split(r"\s{2,}|\|", m.group(1))[0].strip() if m else sender_name
    merchant = (merchant or sender_name or "-")[:40]
    return {
        "amount": -amount if canceled else amount,
        "currency": cur,
        "merchant": merchant,
        "category": cls.category(merchant, subj, sender_name),
        "canceled": canceled,
    }


# ─────────────────────────── Reservation ───────────────────────────
DATE_PATTERNS = [
    (re.compile(r"(20\d{2})\s*[.\-/년年]\s*(\d{1,2})\s*[.\-/월月]\s*(\d{1,2})"), "ymd"),
    (re.compile(r"(?<![\d.])(\d{1,2})\s*[월月]\s*(\d{1,2})\s*[일日]"), "md"),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})"), "mdy_en"),
    (re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+(20\d{2})"), "dmy_en"),
]
TIME_RE = re.compile(r"(?:(오전|오후|AM|PM|am|pm)\s*)?(\d{1,2}):(\d{2})(?:\s*(AM|PM|am|pm))?")
# "15時30分", "午後3時", "오후 3시 30분", "10時半" (時間/시간 = 소요시간은 제외)
TIME_CJK_RE = re.compile(r"(오전|오후|午前|午後)?\s*(\d{1,2})\s*[時시](?![間간])\s*(?:(\d{1,2})\s*[分분]|([半반]))?")


def find_dates(text: str, received: dt.date) -> list[tuple[int, int, dt.date]]:
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
    seg = text[pos:pos + 40]
    m = TIME_RE.search(seg)
    mc = TIME_CJK_RE.search(seg)
    if mc and (not m or mc.start() < m.start()):
        hh = int(mc.group(2))
        mm = int(mc.group(3)) if mc.group(3) else (30 if mc.group(4) else 0)
        ap = (mc.group(1) or "").lower()
    elif m:
        hh, mm = int(m.group(2)), int(m.group(3))
        ap = (m.group(1) or m.group(4) or "").lower()
    else:
        return None
    if ap in ("오후", "午後", "pm") and hh < 12:
        hh += 12
    if ap in ("오전", "午前", "am") and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def parse_reservation(subj: str, body: str, sender_name: str, received: dt.date, cls: Classifier) -> dict | None:
    body = unicodedata.normalize("NFKC", body)  # 전각 숫자/콜론 → 반각
    text = unicodedata.normalize("NFKC", subj) + "\n" + body[:8000]
    dates = find_dates(text, received)
    if not dates:
        return None
    future = [x for x in dates if x[2] >= received]
    _, end, d = (future or dates)[0]
    labels = "|".join(map(re.escape, cls.r["place_labels"]))
    pm = re.search(rf"(?:{labels})\s*[:：]?\s*([^\n]{{2,60}})", body[:8000])
    return {
        "eventDate": d.isoformat(),
        "eventTime": parse_time_near(text, end),
        "place": pm.group(1).strip()[:60] if pm else None,
        "title": re.sub(r"\s+", " ", subj).strip()[:80],
        "vendor": (sender_name or "-")[:40],
        "status": "canceled" if cls.has_any(subj, cls.r["cancel_words"]) else "confirmed",
    }
