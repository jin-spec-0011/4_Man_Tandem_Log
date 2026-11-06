"""숨긴 예약 복원 (1회성 수동 실행용).

GitHub Actions 'unhide-reservations' 워크플로에서 실행된다.
  SCOPE=upcoming : 오늘(KST) 이후 예약만 복원 (기본)
  SCOPE=all      : 숨긴 예약 전체 복원
  DRY_RUN=true   : 대상 목록만 출력하고 쓰지 않음
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import KST, Firebase, log  # noqa: E402


def main() -> int:
    db_url = os.getenv("FIREBASE_DB_URL")
    sa_json = os.getenv("FIREBASE_SA_JSON")
    if not db_url or not sa_json:
        log("FIREBASE_DB_URL / FIREBASE_SA_JSON 미설정")
        return 1

    scope = (os.getenv("SCOPE") or "upcoming").strip().lower()
    if scope not in ("upcoming", "all"):
        log(f"SCOPE 값 오류: {scope} (upcoming | all)")
        return 1
    dry = (os.getenv("DRY_RUN") or "false").strip().lower() == "true"
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")

    store = Firebase(db_url, sa_json)
    res = store.get("pa/reservations", {}) or {}
    if not isinstance(res, dict):
        log("pa/reservations 형식 오류")
        return 1

    targets = []
    for k, v in res.items():
        if not isinstance(v, dict) or not v.get("hidden"):
            continue
        ev = str(v.get("eventDate") or "")
        if scope == "upcoming" and (not ev or ev < today):
            continue
        targets.append((k, v))

    targets.sort(key=lambda kv: str(kv[1].get("eventDate") or ""))
    log(f"기준일 {today} · 범위 {scope} · 숨김 전체 "
        f"{sum(1 for v in res.values() if isinstance(v, dict) and v.get('hidden'))}건 · 복원 대상 {len(targets)}건")
    for k, v in targets:
        log(f"  {v.get('eventDate', '-')} {v.get('eventTime', '') or ''} | {v.get('title', '(제목 없음)')} | {k}")

    if not targets:
        log("복원할 예약 없음")
        return 0
    if dry:
        log("DRY_RUN=true → 쓰기 생략")
        return 0

    store.patch("pa/reservations", {f"{k}/hidden": None for k, _ in targets})
    log(f"복원 완료 {len(targets)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
