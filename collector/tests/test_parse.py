import datetime as dt
import json
import os

from parse import Classifier, parse_reservation, parse_spend, unsub_fields
from store import DryRunStore, compact

RULES = json.load(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "rules.json"), encoding="utf-8"))
CLS = Classifier(RULES)


def test_spend_krw_label_and_category():
    body = "신한카드 승인\n승인금액 12,300원\n가맹점명 스타벅스 강남점\n누적금액 1,000,000원"
    sp = parse_spend("[신한카드] 승인 안내", body, "신한카드", CLS)
    assert sp["amount"] == 12300 and sp["currency"] == "KRW"
    assert sp["merchant"].startswith("스타벅스") and sp["category"] == "식비"


def test_spend_cancel_negative_and_usd():
    sp = parse_spend("결제 취소 안내", "결제금액 5,000원", "쿠팡", CLS)
    assert sp["amount"] == -5000 and sp["canceled"]
    sp2 = parse_spend("Your receipt from Anthropic", "Total $20.00", "Anthropic", CLS)
    assert sp2["currency"] == "USD" and sp2["amount"] == 20.0 and sp2["category"] == "구독"


def test_reservation_ko_ja():
    rcv = dt.date(2026, 11, 1)
    r = parse_reservation("예약 확정", "이용일 2026.11.20 오후 3:30\n장소: 강남 테니스장", "ABC", rcv, CLS)
    assert r["eventDate"] == "2026-11-20" and r["eventTime"] == "15:30" and r["place"].startswith("강남")
    j = parse_reservation("ご予約確定", "２０２６年１２月３日 午後３時\n施設名：ホテル", "楽天", rcv, CLS)
    assert j["eventDate"] == "2026-12-03" and j["eventTime"] == "15:00"
    c = parse_reservation("예약 취소", "12월 5일", "ABC", rcv, CLS)
    assert c["status"] == "canceled" and c["eventDate"] == "2026-12-05"


def test_ad_detection():
    assert CLS.is_ad("(광고) 특가", "x@shop.com", "", False)
    assert CLS.is_ad("신상품 소개", "x@shop.com", "<https://u>", False)
    assert CLS.is_ad("주간 소식", "x@shop.com", "", True)
    assert not CLS.is_ad("결제 영수증", "x@shop.com", "<https://u>", True)
    assert not CLS.is_ad("공지", "a@shinhancard.com", "<https://u>", False)


def test_unsub_fields():
    f = unsub_fields("<mailto:u@x.com>, <https://x.com/u>", "List-Unsubscribe=One-Click")
    assert f == {"unsubHttp": "https://x.com/u", "unsubMailto": "mailto:u@x.com", "oneClick": True}


def test_compact_merges_conflicts():
    out = compact({"a/b": {"x": 1}, "a/b/y": 2, "c": None, "c/d": 3, "e/f": 1})
    assert out == {"a/b": {"x": 1, "y": 2}, "c": None, "e/f": 1}


def test_dryrun_store(tmp_path):
    s = DryRunStore(str(tmp_path / "o.json"))
    s.patch({"a/b/c": 1, "a/d": {"e": 2}})
    assert s.get("a/b/c") == 1 and s.get("a", shallow=True) == {"b": True, "d": True}
    s.patch({"a/b": None})
    assert s.get("a/b") is None
