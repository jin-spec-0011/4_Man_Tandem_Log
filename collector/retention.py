"""숨김·차단 항목 보관 정책.

- 보관 기간: 숨김/차단 시점(actionAt)부터 RETAIN_DAYS(180일)
- 사전 경고: 만료 WARN_DAYS(7일) 전부터 대시보드 배너 + 경고 메일 1회
- 삭제: 만료 시 승인 없이 자동 삭제 (DB 레코드). 메일 자체의 영구 삭제는 각 메일함의
  휴지통 규칙(Gmail 30일 등)을 따른다 → 수집기는 메일을 영구 삭제하지 않는다.

인덱스 구조  mail/retention/{rid}
  {"kind": "hidden"|"blocked", "path": "spend/2026-11/abcd", "label": "표시용", "actionAt": ISO,
   "expiresAt": ISO, "warnedAt": ISO|None}
rid 는 path 로부터 결정적으로 생성 (path_to_rid) → 대시보드/수집기 양쪽에서 동일 키 사용.
"""
from __future__ import annotations

import datetime as dt
import re

RETAIN_DAYS = 180
WARN_DAYS = 7
ALLOWED_ROOTS = ("daily/", "spend/", "reservations/", "adSenders/")
_PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]{3,200}$")


def path_to_rid(path: str) -> str:
    """RTDB 키에 허용되지 않는 '/' 를 '~' 로 치환 (app.js 의 pathToRid 와 동일 규칙)."""
    return path.strip("/").replace("/", "~")


def valid_path(path: str) -> bool:
    return bool(_PATH_RE.match(path or "")) and path.startswith(ALLOWED_ROOTS) and ".." not in path


def parse_iso(s) -> dt.datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        v = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=dt.timezone.utc)
    return v


def expires_of(action_at: dt.datetime) -> dt.datetime:
    return action_at + dt.timedelta(days=RETAIN_DAYS)


def classify(entry: dict, now: dt.datetime) -> str:
    """'invalid' | 'active' | 'warn' | 'expired'"""
    if not isinstance(entry, dict) or not valid_path(entry.get("path", "")):
        return "invalid"
    act = parse_iso(entry.get("actionAt"))
    if act is None:
        return "invalid"
    exp = expires_of(act)  # expiresAt 필드는 표시용. 판정은 항상 actionAt 기준으로 재계산
    if now >= exp:
        return "expired"
    if now >= exp - dt.timedelta(days=WARN_DAYS):
        return "warn"
    return "active"


def plan(index: dict, now: dt.datetime) -> dict:
    """인덱스를 판정해 처리 계획을 반환. 순수 함수(테스트 대상)."""
    out = {"expired": [], "warn_new": [], "warn": [], "invalid": [], "fix": {}}
    for rid, e in (index or {}).items():
        state = classify(e, now)
        if state == "invalid":
            out["invalid"].append(rid)
            continue
        act = parse_iso(e["actionAt"])
        exp_iso = expires_of(act).isoformat()
        if e.get("expiresAt") != exp_iso:
            out["fix"][f"{rid}/expiresAt"] = exp_iso
        if state == "expired":
            out["expired"].append((rid, e))
        elif state == "warn":
            out["warn"].append((rid, e))
            warned = parse_iso(e.get("warnedAt"))
            # 연장(actionAt 갱신) 후 다시 경고 구간에 들어오면 재경고
            if warned is None or warned < act:
                out["warn_new"].append((rid, e))
    out["warn"].sort(key=lambda x: x[1]["actionAt"])
    out["warn_new"].sort(key=lambda x: x[1]["actionAt"])
    return out


def days_left(entry: dict, now: dt.datetime) -> int:
    act = parse_iso(entry.get("actionAt"))
    if act is None:
        return 0
    remain = expires_of(act) - now
    return max(0, remain.days + (1 if remain.seconds or remain.microseconds else 0))
