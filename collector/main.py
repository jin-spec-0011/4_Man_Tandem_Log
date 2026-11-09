#!/usr/bin/env python3
"""개인 메일 대시보드 수집기 (GitHub Actions 일 1회).

처리 순서
  1) 보관 정책: 만료(180일) 숨김·차단 항목 자동 삭제 → 인덱스 정합성 보정
  2) 계정별: 차단 발신자 처리(원클릭 수신거부 1회 + 메일 휴지통 이동)
             헤더 스캔(30일) → 저관심 광고 발신자 = 차단 요청 목록
             수집 윈도우 → 일별 정리 / 지출 / 예약 추출
  3) 만료 7일 전 항목 경고 메일 1회 (Gmail 본인 주소) + Actions 경고
  4) meta/lastRun 기록, Job Summary 출력

환경변수 (모두 GitHub Secrets 참조)
  GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET     OAuth 클라이언트 (데스크톱 앱 유형)
  GMAIL1_REFRESH_TOKEN / GMAIL2_REFRESH_TOKEN
  NAVER_IMAP_USER / NAVER_IMAP_PASSWORD      네이버 아이디 / 애플리케이션 비밀번호
  FIREBASE_DB_URL / FIREBASE_SA_JSON
  WINDOW_DAYS (선택)                         수집 기간, 기본 rules.json window_days, 최대 120
  DRY_RUN=1  (선택)                          Firebase 대신 collector/out.json
  ONLY_ACCOUNTS (선택)                       "g1,nv" 처럼 일부 계정만
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import retention as R  # noqa: E402
from parse import KST, Classifier, h16, parse_reservation, parse_spend, unsub_fields  # noqa: E402
from sources import GmailSource, ImapSource, SourceAuthError, unsubscribe_oneclick  # noqa: E402
from store import DryRunStore, Firebase, StoreError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def log(*a):
    print(f"[{dt.datetime.now(KST):%H:%M:%S}]", *a, flush=True)


def gh_warning(msg: str):
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}".replace("\n", " "), flush=True)


def summary(md: str):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    except OSError:
        pass


# ─────────────────────────── Config ───────────────────────────
def load_rules() -> dict:
    with open(os.path.join(HERE, "rules.json"), encoding="utf-8") as f:
        rules = json.load(f)
    for k in ("window_days", "interest_scan_days", "block_rule", "accounts", "categories"):
        if k not in rules:
            raise ValueError(f"rules.json 에 '{k}' 누락")
    return rules


def resolve_accounts(rules: dict) -> tuple[list[dict], list[str]]:
    """rules.accounts + 환경변수 → 실행 가능한 계정 목록. 누락 계정은 사유와 함께 skip."""
    only = {x.strip() for x in (os.getenv("ONLY_ACCOUNTS") or "").split(",") if x.strip()}
    cid, csec = os.getenv("GMAIL_CLIENT_ID", ""), os.getenv("GMAIL_CLIENT_SECRET", "")
    ok, skipped = [], []
    for aid, a in rules["accounts"].items():
        if not ID_RE.match(aid):
            skipped.append(f"{aid}: id 형식 오류")
            continue
        if only and aid not in only:
            continue
        if a.get("kind") == "gmail":
            rt = os.getenv(a.get("token_env", ""), "")
            if not (cid and csec and rt):
                skipped.append(f"{aid}: GMAIL_CLIENT_ID/SECRET 또는 {a.get('token_env')} 미설정")
                continue
            ok.append({"id": aid, "label": a.get("label", aid), "kind": "gmail", "rt": rt})
        elif a.get("kind") == "imap":
            user, pw = os.getenv(a.get("user_env", ""), ""), os.getenv(a.get("pass_env", ""), "")
            if not (user and pw and a.get("host")):
                skipped.append(f"{aid}: {a.get('user_env')}/{a.get('pass_env')} 미설정")
                continue
            ok.append({"id": aid, "label": a.get("label", aid), "kind": "imap", "host": a["host"],
                       "port": int(a.get("port", 993)), "user": user, "pw": pw})
        else:
            skipped.append(f"{aid}: kind 는 gmail|imap")
    return ok, skipped


def open_source(acc: dict):
    if acc["kind"] == "gmail":
        return GmailSource(acc["id"], os.environ["GMAIL_CLIENT_ID"], os.environ["GMAIL_CLIENT_SECRET"], acc["rt"])
    return ImapSource(acc["id"], acc["host"], acc["user"], acc["pw"], acc["port"])


# ─────────────────────────── Retention ───────────────────────────
def label_of(path: str, node) -> str:
    if not isinstance(node, dict):
        return path
    for k in ("title", "subject", "merchant", "name", "sender"):
        if node.get(k):
            return str(node[k])[:80]
    return path


def apply_retention(store, now: dt.datetime, ad_senders: dict, reservations: dict, updates: dict) -> dict:
    index = store.get("retention", {}) or {}
    if not isinstance(index, dict):
        index = {}

    # 자기 치유: 상태값은 있는데 인덱스가 없는 차단/숨김 항목 → 인덱스 생성
    def ensure(path: str, kind: str, node: dict, at_field: str):
        rid = R.path_to_rid(path)
        if rid in index:
            return
        act = R.parse_iso(node.get(at_field)) or now
        entry = {"kind": kind, "path": path, "label": label_of(path, node), "actionAt": act.isoformat(),
                 "expiresAt": R.expires_of(act).isoformat()}
        index[rid] = entry
        updates[f"retention/{rid}"] = entry
        updates[f"{path}/{at_field}"] = act.isoformat()

    for key, s in ad_senders.items():
        if isinstance(s, dict) and s.get("status") == "blocked":
            ensure(f"adSenders/{key}", "blocked", s, "blockedAt")
        elif isinstance(s, dict) and s.get("status") == "dismissed":
            ensure(f"adSenders/{key}", "hidden", s, "hiddenAt")
    for key, r in reservations.items():
        if isinstance(r, dict) and r.get("hidden"):
            ensure(f"reservations/{key}", "hidden", r, "hiddenAt")

    p = R.plan(index, now)
    purged = {"hidden": 0, "blocked": 0}
    for rid in p["invalid"]:
        updates[f"retention/{rid}"] = None
    for rid, e in p["expired"]:
        path = e["path"]
        updates[path] = None
        updates[f"retention/{rid}"] = None
        purged[e.get("kind", "hidden")] = purged.get(e.get("kind", "hidden"), 0) + 1
        parts = path.split("/")
        if parts[0] == "adSenders" and len(parts) == 2:
            ad_senders.pop(parts[1], None)
        if parts[0] == "reservations" and len(parts) == 2:
            reservations.pop(parts[1], None)
        log(f"  보관 만료 삭제: {e.get('kind')} {path} ({e.get('label', '')})")
    for k, v in p["fix"].items():
        if k.split("/")[0] not in {rid for rid, _ in p["expired"]}:
            updates[f"retention/{k}"] = v
    expired = {rid for rid, _ in p["expired"]} | set(p["invalid"])
    return {"purged": purged, "warn": p["warn"], "warn_new": p["warn_new"], "invalid": len(p["invalid"]),
            "rids": {rid for rid in index if rid not in expired}}


def warn_text(items: list, now: dt.datetime, dash_url: str) -> str:
    lines = ["보관 기간(180일) 만료로 아래 숨김·차단 항목이 승인 없이 자동 삭제됩니다.",
             "유지하려면 대시보드 '보관함'에서 [연장] 하세요. 차단 항목은 삭제 시 차단도 해제됩니다.", ""]
    for _, e in items:
        exp = R.expires_of(R.parse_iso(e["actionAt"])).astimezone(KST)
        lines.append(f"- D-{R.days_left(e, now)} ({exp:%Y-%m-%d}) [{'차단' if e.get('kind') == 'blocked' else '숨김'}] "
                     f"{e.get('label', e['path'])}")
    if dash_url:
        lines += ["", f"대시보드: {dash_url}#vault"]
    return "\n".join(lines)


# ─────────────────────────── Per-account ───────────────────────────
def process_account(src, acc: dict, rules: dict, cls: Classifier, store, ctx: dict, updates: dict) -> dict:
    aid = acc["id"]
    now = ctx["now"]
    st = {"ok": True, "scanned": 0, "spend": 0, "reservations": 0, "candidates": 0,
          "trashed": 0, "unsubscribed": 0}

    # 1) 차단 발신자: 원클릭 수신거부(최초 1회) + 기존/신규 메일 휴지통 이동
    for key, s in ctx["ad_senders"].items():
        if not isinstance(s, dict) or s.get("account") != aid or s.get("status") != "blocked":
            continue
        if not s.get("unsub"):
            res = unsubscribe_oneclick(s.get("unsubHttp")) if s.get("oneClick") else {
                "method": "manual_link" if s.get("unsubHttp") else ("manual_mailto" if s.get("unsubMailto") else "none"),
                "ok": False}
            res["at"] = now.isoformat()
            updates[f"adSenders/{key}/unsub"] = res
            st["unsubscribed"] += 1 if res.get("ok") else 0
        try:
            n = src.trash_from(s.get("sender", ""), int(rules.get("trash_limit_per_sender", 500)))
        except SourceAuthError:
            raise
        except Exception as e:
            log(f"  [{aid}] 휴지통 이동 실패 {s.get('sender')}: {type(e).__name__}")
            n = 0
        if n:
            updates[f"adSenders/{key}/trashed"] = int(s.get("trashed") or 0) + n
            updates[f"adSenders/{key}/lastTrashAt"] = now.isoformat()
            st["trashed"] += n
            log(f"  [{aid}] 차단 {s.get('sender')} → 휴지통 {n}건")

    # 2) 헤더 스캔
    win = ctx["window_days"]
    scan_days = max(int(rules["interest_scan_days"]), win)
    since_scan = (now - dt.timedelta(days=scan_days - 1)).date()
    win_start = (now - dt.timedelta(days=win - 1)).date()
    headers = src.headers_since(since_scan)
    st["scanned"] = len(headers)
    for h in headers:
        h["ad"] = cls.is_ad(h["subject"], h["from"], h["list_unsub"], h.get("promo", False))

    # 3) 저관심 광고 발신자 → 차단 요청 목록
    senders: dict[str, dict] = {}
    for h in headers:
        if not h["ad"] or not h["from"]:
            continue
        s = senders.setdefault(h["from"], {"name": h["from_name"], "total": 0, "read": 0, "last": h["date"],
                                           "subject": h["subject"], "lu": "", "lup": ""})
        s["total"] += 1
        s["read"] += 1 if h["seen"] else 0
        if h["date"] >= s["last"]:
            s.update(last=h["date"], subject=h["subject"], name=h["from_name"])
        if h["list_unsub"] and not s["lu"]:
            s.update(lu=h["list_unsub"], lup=h["list_unsub_post"])
    br = rules["block_rule"]
    for addr, s in senders.items():
        key = h16(f"{aid}|{addr}")
        ex = ctx["ad_senders"].get(key)
        if isinstance(ex, dict) and ex.get("status") in ("blocked", "dismissed"):
            continue
        rate = s["read"] / s["total"]
        if s["total"] >= br["min_ad_mails"] and rate <= br["max_read_rate"]:
            rec = {"account": aid, "sender": addr, "name": s["name"][:60], "total": s["total"], "read": s["read"],
                   "rate": round(rate, 3), "lastSubject": s["subject"][:100], "lastAt": s["last"].isoformat(),
                   "status": "pending", "updatedAt": now.isoformat(), **unsub_fields(s["lu"], s["lup"])}
            rec["firstSeenAt"] = (ex or {}).get("firstSeenAt") or now.isoformat()
            if isinstance(ex, dict):
                # 기존 후보: status 는 건드리지 않음 (수집 중 대시보드에서 차단/유지한 결과 보호)
                for f, v in rec.items():
                    if f != "status":
                        updates[f"adSenders/{key}/{f}"] = v
            else:
                updates[f"adSenders/{key}"] = rec
            ctx["ad_senders"][key] = rec
            st["candidates"] += 1
        elif isinstance(ex, dict) and ex.get("status") == "pending":
            updates[f"adSenders/{key}"] = None  # 다시 읽기 시작 → 후보 해제
            ctx["ad_senders"].pop(key, None)

    # 4) 일별 정리 + 지출/예약 추출
    days: dict[str, dict] = {}
    for i in range(win):
        d = (win_start + dt.timedelta(days=i)).isoformat()
        prev = store.get(f"daily/{d}/{aid}/items", {}) or {}
        days[d] = {"total": 0, "unread": 0, "ads": 0, "spendKRW": 0, "reservations": 0, "items": {},
                   "_prev": prev if isinstance(prev, dict) else {}, "_n": 0, "_na": 0}
    item_lim, ad_lim = int(rules.get("daily_item_limit", 40)), int(rules.get("daily_ad_item_limit", 20))

    for h in sorted(headers, key=lambda x: x["date"], reverse=True):
        dkey = h["date"].date().isoformat()
        day = days.get(dkey)
        if day is None:
            continue
        mkey = h16(h["msgid"] or f"{aid}|{h['id']}|{h['subject']}")
        day["total"] += 1
        day["unread"] += 0 if h["seen"] else 1
        day["ads"] += 1 if h["ad"] else 0
        tag = "ad" if h["ad"] else "mail"

        if not h["ad"] and not cls.is_ad_marked(h["subject"]):
            is_sp, is_rs = cls.is_spend_subject(h["subject"]), cls.is_res_subject(h["subject"])
            month = h["date"].strftime("%Y-%m")
            known_sp = is_sp and mkey in ctx["spend_keys"](month)
            known_rs = is_rs and mkey in ctx["reservations"]
            if known_sp:
                tag = "spend"
            elif known_rs:
                tag = "res"
            elif is_sp or is_rs:
                try:
                    body = src.body(h["id"])
                except SourceAuthError:
                    raise
                except Exception as e:
                    log(f"  [{aid}] 본문 조회 실패 {h['id']}: {type(e).__name__}")
                    body = ""
                sp = parse_spend(h["subject"], body, h["from_name"], cls) if is_sp and body else None
                if sp and sp.get("amount"):
                    updates[f"spend/{month}/{mkey}"] = {
                        **sp, "date": dkey, "time": h["date"].strftime("%H:%M"), "account": aid,
                        "subject": h["subject"][:100], "from": h["from_name"][:40]}
                    ctx["spend_keys"](month)[mkey] = True
                    if sp["currency"] == "KRW":
                        day["spendKRW"] += sp["amount"]
                    st["spend"] += 1
                    tag = "spend"
                elif is_rs and body:
                    rs = parse_reservation(h["subject"], body, h["from_name"], h["date"].date(), cls)
                    if rs:
                        rec = {**rs, "account": aid, "receivedAt": h["date"].isoformat(), "subject": h["subject"][:100]}
                        if rs["status"] == "canceled":
                            for rk, rv in ctx["reservations"].items():
                                if (isinstance(rv, dict) and rv.get("vendor") == rs["vendor"]
                                        and rv.get("eventDate") == rs["eventDate"] and rv.get("status") != "canceled"):
                                    rv["status"] = "canceled"
                                    updates[f"reservations/{rk}/status"] = "canceled"
                        updates[f"reservations/{mkey}"] = rec
                        ctx["reservations"][mkey] = rec
                        day["reservations"] += 1
                        st["reservations"] += 1
                        tag = "res"

        if tag == "ad":
            if day["_na"] >= ad_lim:
                continue
            day["_na"] += 1
        else:
            if day["_n"] >= item_lim:
                continue
            day["_n"] += 1
        item = {"from": h["from_name"][:40], "addr": h["from"][:80], "subject": h["subject"][:100],
                "time": h["date"].strftime("%H:%M"), "unread": not h["seen"], "tag": tag}
        old = day["_prev"].get(mkey)
        rid = R.path_to_rid(f"daily/{dkey}/{aid}/items/{mkey}")
        if (isinstance(old, dict) and old.get("hidden")) or rid in ctx["hidden_rids"]:  # 숨김 상태 보존
            item["hidden"] = True
            item["hiddenAt"] = (old or {}).get("hiddenAt") if isinstance(old, dict) else None
            item["hiddenAt"] = item["hiddenAt"] or now.isoformat()
        day["items"][mkey] = item

    for dkey, day in days.items():
        prev = day.pop("_prev")
        day.pop("_n"), day.pop("_na")
        # 이번 스캔에 없지만 숨김 중인 항목(메일함에서 이동/삭제됨)은 보관 기간 동안 유지
        for k, v in prev.items():
            if k not in day["items"] and isinstance(v, dict) and v.get("hidden"):
                day["items"][k] = v
        day["spendKRW"] = round(day["spendKRW"])
        updates[f"daily/{dkey}/{aid}"] = day
    return st


# ─────────────────────────── Main ───────────────────────────
def main() -> int:
    try:
        rules = load_rules()
    except (OSError, ValueError) as e:
        log(f"rules.json 오류: {e}")
        return 2
    accounts, skipped = resolve_accounts(rules)
    for s in skipped:
        log(f"계정 건너뜀 — {s}")
        gh_warning(f"계정 건너뜀: {s}")
    if not accounts:
        log("실행 가능한 계정 없음 (Secrets 확인)")
        return 2

    try:
        if os.getenv("DRY_RUN") == "1":
            store = DryRunStore(os.path.join(HERE, "out.json"))
        else:
            url, sa = os.getenv("FIREBASE_DB_URL", ""), os.getenv("FIREBASE_SA_JSON", "")
            if not url or not sa:
                log("FIREBASE_DB_URL / FIREBASE_SA_JSON 미설정")
                return 2
            store = Firebase(url, sa)
    except StoreError as e:
        log(str(e))
        return 2

    now = dt.datetime.now(KST)
    try:
        win = max(1, min(int(os.getenv("WINDOW_DAYS") or rules["window_days"]), 120))
    except ValueError:
        log("WINDOW_DAYS 는 정수여야 합니다")
        return 2

    spend_cache: dict[str, dict] = {}

    def spend_keys(month: str) -> dict:
        if month not in spend_cache:
            spend_cache[month] = dict.fromkeys((store.get(f"spend/{month}", {}, shallow=True) or {}).keys(), True)
        return spend_cache[month]

    try:
        ad_senders = store.get("adSenders", {}) or {}
        reservations = store.get("reservations", {}) or {}
    except StoreError as e:
        log(str(e))
        return 2

    # 1) 보관 정책
    ret_updates: dict = {}
    ret = apply_retention(store, now, ad_senders, reservations, ret_updates)
    if ret_updates:
        store.patch(ret_updates)
    log(f"보관 정책: 삭제 숨김 {ret['purged'].get('hidden', 0)} / 차단 {ret['purged'].get('blocked', 0)}, "
        f"만료 임박 {len(ret['warn'])}")

    ctx = {"now": now, "window_days": win, "ad_senders": ad_senders, "reservations": reservations,
           "spend_keys": spend_keys, "hidden_rids": ret["rids"]}
    cls = Classifier(rules)
    run = {"startedAt": now.isoformat(), "windowDays": win, "accounts": {}, "skipped": skipped}
    failed = 0
    notifier = None

    # 2) 계정별 처리
    for acc in accounts:
        aid = acc["id"]
        log(f"계정 처리 시작: {aid} ({acc['kind']})")
        updates: dict = {}
        src = None
        try:
            src = open_source(acc)
            st = process_account(src, acc, rules, cls, store, ctx, updates)
            if aid == rules.get("notify_account") and acc["kind"] == "gmail":
                notifier = src
            log(f"  [{aid}] 완료: {st}")
        except SourceAuthError as e:
            failed += 1
            st = {"ok": False, "error": str(e)[:300], "auth": True}
            log(f"  [{aid}] 인증 오류: {e}")
            gh_warning(f"[{aid}] {e}")
        except Exception as e:
            failed += 1
            st = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:250]}"}
            log(f"  [{aid}] 실패:\n{traceback.format_exc()}")
        finally:
            if src is not None and src is not notifier:
                src.close()
        run["accounts"][aid] = st
        updates[f"accounts/{aid}"] = {"label": acc["label"], "kind": acc["kind"], "ok": st.get("ok", False),
                                      "at": now.isoformat(), "error": st.get("error")}
        try:
            store.patch(updates)
        except StoreError as e:
            failed += 1
            log(f"  [{aid}] 저장 실패: {e}")

    # 3) 만료 7일 전 경고 (항목당 1회, 연장 후 재진입 시 재경고)
    warn_sent = False
    if ret["warn_new"]:
        text = warn_text(ret["warn_new"], now, os.getenv("DASHBOARD_URL", ""))
        for _, e in ret["warn_new"]:
            gh_warning(f"보관 만료 D-{R.days_left(e, now)}: {e.get('label', e['path'])}")
        if notifier is not None:
            try:
                warn_sent = notifier.send_self(f"[메일 대시보드] 숨김·차단 {len(ret['warn_new'])}건 7일 내 자동 삭제 예정", text)
            except Exception as e:
                log(f"경고 메일 발송 실패: {type(e).__name__}: {e}")
        notify_expected = any(a["id"] == rules.get("notify_account") and a["kind"] == "gmail" for a in accounts)
        if warn_sent or not notify_expected:
            # 메일 발송 성공, 또는 알림 계정 미구성(대시보드 배너로만 경고) → 경고 완료 처리
            # 알림 계정이 있는데 실패한 경우는 다음 실행에서 재발송
            store.patch({f"retention/{rid}/warnedAt": now.isoformat() for rid, _ in ret["warn_new"]})
        log(f"만료 경고 {len(ret['warn_new'])}건 · 메일 {'발송' if warn_sent else '미발송'}")
    if notifier is not None:
        notifier.close()

    run["finishedAt"] = dt.datetime.now(KST).isoformat()
    run["ok"] = failed == 0
    run["retention"] = {"purged": ret["purged"], "warn": len(ret["warn"]), "warnMailSent": warn_sent}
    store.patch({"meta/lastRun": run})

    md = ["## 메일 수집 결과", "", "| 계정 | 상태 | 스캔 | 지출 | 예약 | 차단요청 | 휴지통 |", "|---|---|---|---|---|---|---|"]
    for aid, st in run["accounts"].items():
        if st.get("ok"):
            md.append(f"| {aid} | OK | {st['scanned']} | {st['spend']} | {st['reservations']} | "
                      f"{st['candidates']} | {st['trashed']} |")
        else:
            md.append(f"| {aid} | 실패 | - | - | - | - | - |")
    md += ["", f"보관 만료 삭제: 숨김 {ret['purged'].get('hidden', 0)} · 차단 {ret['purged'].get('blocked', 0)} "
               f"/ 7일 내 만료 예정: {len(ret['warn'])}"]
    for s in skipped:
        md.append(f"- 건너뜀: {s}")
    for aid, st in run["accounts"].items():
        if not st.get("ok"):
            md.append(f"- **{aid}** 오류: {st.get('error')}")
    summary("\n".join(md))
    log(f"종료: 실패 {failed}/{len(accounts)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
