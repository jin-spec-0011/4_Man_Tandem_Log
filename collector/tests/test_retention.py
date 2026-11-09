import datetime as dt

import retention as R

NOW = dt.datetime(2026, 11, 9, 7, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def entry(days_ago, **kw):
    e = {"kind": "hidden", "path": "spend/2026-05/abc", "actionAt": (NOW - dt.timedelta(days=days_ago)).isoformat()}
    e.update(kw)
    return e


def test_classify_boundaries():
    assert R.classify(entry(10), NOW) == "active"
    assert R.classify(entry(172), NOW) == "active"       # D-8
    assert R.classify(entry(173), NOW) == "warn"         # D-7
    assert R.classify(entry(179.9), NOW) == "warn"
    assert R.classify(entry(180), NOW) == "expired"
    assert R.classify({"path": "../x", "actionAt": NOW.isoformat()}, NOW) == "invalid"
    assert R.classify({"path": "meta/lastRun", "actionAt": NOW.isoformat()}, NOW) == "invalid"
    assert R.classify({"path": "spend/x/y", "actionAt": "bad"}, NOW) == "invalid"


def test_plan_warns_once_and_rewarns_after_extend():
    idx = {
        "a": entry(175),
        "b": entry(175, warnedAt=(NOW - dt.timedelta(days=1)).isoformat()),
        "c": entry(200),
        "d": entry(3, warnedAt=(NOW - dt.timedelta(days=10)).isoformat()),  # 연장됨, 경고 구간 아님
        "e": entry(174, warnedAt=(NOW - dt.timedelta(days=300)).isoformat()),  # 연장 전 경고 → 재경고
    }
    p = R.plan(idx, NOW)
    assert [r for r, _ in p["expired"]] == ["c"]
    assert sorted(r for r, _ in p["warn"]) == ["a", "b", "e"]
    assert sorted(r for r, _ in p["warn_new"]) == ["a", "e"]
    assert "a/expiresAt" in p["fix"]


def test_rid_and_days_left():
    assert R.path_to_rid("daily/2026-11-01/g1/items/k") == "daily~2026-11-01~g1~items~k"
    assert R.days_left(entry(173), NOW) == 7
    assert R.days_left(entry(179.5), NOW) == 1
    assert R.days_left(entry(181), NOW) == 0
