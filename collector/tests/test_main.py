import datetime as dt
import json

import main as M
from parse import KST


class FakeSrc:
    kind = "gmail"

    def __init__(self, aid):
        self.id = aid
        self.trashed = []
        self.sent = []
        now = dt.datetime.now(KST).replace(microsecond=0)
        self.h = [
            {"id": "1", "seen": False, "date": now, "from_name": "신한카드", "from": "noti@shinhancard.com",
             "subject": "[신한카드] 승인 안내", "msgid": "<m1>", "list_unsub": "", "list_unsub_post": "", "promo": False},
            {"id": "2", "seen": True, "date": now, "from_name": "ABC", "from": "r@abc.com",
             "subject": "예약 확정 안내", "msgid": "<m2>", "list_unsub": "", "list_unsub_post": "", "promo": False},
        ] + [
            {"id": f"a{i}", "seen": False, "date": now - dt.timedelta(days=i), "from_name": "Shop", "from": "ad@shop.com",
             "subject": "(광고) 특가", "msgid": f"<a{i}>", "list_unsub": "<https://shop.com/u>",
             "list_unsub_post": "List-Unsubscribe=One-Click", "promo": True} for i in range(4)
        ]

    def headers_since(self, since):
        return [x for x in self.h if x["date"].date() >= since]

    def body(self, mid):
        return {"1": "승인금액 12,300원\n가맹점명 스타벅스", "2": "이용일 2099.01.02 10:00"}.get(mid, "")

    def trash_from(self, sender, limit=500):
        self.trashed.append(sender)
        return 2

    def send_self(self, s, t):
        self.sent.append(s)
        return True

    def close(self):
        pass


def test_end_to_end(tmp_path, monkeypatch):
    out = tmp_path / "out.json"
    now = dt.datetime.now(KST)
    old = (now - dt.timedelta(days=181)).isoformat()
    warn = (now - dt.timedelta(days=175)).isoformat()
    seed = {
        "adSenders": {
            "blk": {"account": "g1", "sender": "spam@x.com", "status": "blocked", "blockedAt": now.isoformat(),
                    "oneClick": False},
            "exp": {"account": "g1", "sender": "old@x.com", "status": "blocked", "blockedAt": old},
        },
        "reservations": {"r1": {"title": "옛 예약", "hidden": True, "hiddenAt": warn}},
        "retention": {
            "adSenders~exp": {"kind": "blocked", "path": "adSenders/exp", "actionAt": old},
            "reservations~r1": {"kind": "hidden", "path": "reservations/r1", "actionAt": warn},
        },
    }
    out.write_text(json.dumps(seed), encoding="utf-8")
    monkeypatch.setattr(M, "HERE", str(tmp_path))
    rules = json.load(open(M.os.path.join(M.os.path.dirname(M.__file__), "rules.json"), encoding="utf-8"))
    (tmp_path / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "x")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "x")
    monkeypatch.setenv("GMAIL1_REFRESH_TOKEN", "x")
    monkeypatch.delenv("GMAIL2_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("NAVER_IMAP_USER", raising=False)
    srcs = {}
    monkeypatch.setattr(M, "open_source", lambda acc: srcs.setdefault(acc["id"], FakeSrc(acc["id"])))

    assert M.main() == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert "exp" not in d["adSenders"] and "adSenders~exp" not in d["retention"]       # 만료 자동 삭제
    assert "adSenders~blk" in d["retention"]                                            # 인덱스 자기 치유
    assert d["adSenders"]["blk"]["unsub"]["method"] == "none"
    assert srcs["g1"].trashed == ["spam@x.com"]                                         # 만료된 old@x.com 은 휴지통 처리 안 함
    assert d["retention"]["reservations~r1"]["warnedAt"] and len(srcs["g1"].sent) == 1  # D-7 경고 1회
    month = now.strftime("%Y-%m")
    assert list(d["spend"][month].values())[0]["amount"] == 12300
    assert any(r.get("eventDate") == "2099-01-02" for r in d["reservations"].values())
    cands = [s for s in d["adSenders"].values() if s.get("status") == "pending"]
    assert cands and cands[0]["sender"] == "ad@shop.com" and cands[0]["oneClick"]
    today = d["daily"][now.date().isoformat()]["g1"]
    assert today["spendKRW"] == 12300 and {i["tag"] for i in today["items"].values()} >= {"spend", "res", "ad"}
    assert d["meta"]["lastRun"]["ok"] is True

    # 2회차: 경고 재발송 없음, 지출 중복 적재 없음
    assert M.main() == 0
    assert len(srcs["g1"].sent) == 1
    d2 = json.loads(out.read_text(encoding="utf-8"))
    assert len(d2["spend"][month]) == 1
