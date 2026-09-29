import datetime as dt
from types import SimpleNamespace

import pytest

from cf import diagnosis, snapshot
from cf.demo import simulated_records
from cf.github_client import normalize
from cf.metrics import analyze, facts_for_llm, pr_frame

T0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)


def h(n):
    return T0 + dt.timedelta(hours=n)


def pr(number, author="a", reviews=(), commit=0, ready=2, merged=30, size=50, bot=False):
    return {
        "number": number, "title": "t", "url": "", "author": author, "is_bot": bot,
        "created_at": h(ready), "ready_at": h(ready), "first_commit_at": h(commit), "merged_at": h(merged),
        "additions": size, "deletions": 0, "changed_files": 1,
        "reviews": [{"author": r, "is_bot": False, "state": s, "submitted_at": h(t)} for r, s, t in reviews],
    }


def test_stage_durations():
    df = pr_frame([pr(1, reviews=[("b", "CHANGES_REQUESTED", 10), ("b", "APPROVED", 20)], merged=21)])
    row = df.iloc[0]
    assert row.coding == 2
    assert row.wait_review == 8
    assert row.review_loop == 10
    assert row.wait_merge == 1
    assert row.rework
    assert row.lead_time == 21


def test_self_reviews_and_bots_ignored():
    records = [
        pr(1, reviews=[("a", "COMMENTED", 3), ("b", "APPROVED", 5)]),
        pr(2, author="dependabot[bot]", bot=True),
    ]
    df = pr_frame(records)
    assert len(df) == 1
    assert df.iloc[0].first_reviewer == "b"
    assert df.iloc[0].wait_review == 3


def test_review_wait_is_the_constraint():
    records = [pr(i, reviews=[("b", "APPROVED", 50)], merged=51) for i in range(10)]
    a = analyze(records, "x/y", 90)
    assert a.constraint == "wait_review"
    assert a.signals["top_reviewer_share"] == 1.0


def test_merge_wait_is_the_constraint():
    records = [pr(i, reviews=[("b", "APPROVED", 4)], merged=80) for i in range(10)]
    assert analyze(records, "x/y", 90).constraint == "wait_merge"


def test_demo_plants_review_constraint_and_report_matches():
    a = analyze(simulated_records(), "demo", 90)
    assert a.constraint == "wait_review"
    assert a.excluded_bots == 9
    report, src = diagnosis.diagnose(a, api_key="")
    assert src == "rules"
    assert "Waiting for review" in report and "Exploit" in report
    assert "—" not in report  # no em dashes


def test_llm_path_gets_only_computed_numbers(monkeypatch):
    a = analyze(simulated_records(), "demo", 90)
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="### The constraint\nok")])

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    report, src = diagnosis.diagnose(a, api_key="sk-test")
    assert src == "claude" and report.startswith("### The constraint")
    assert '"constraint_stage": "Waiting for review"' in seen["messages"][0]["content"]
    assert "Never invent" in seen["system"]


def test_llm_failure_falls_back(monkeypatch):
    import anthropic

    def boom(**kw):
        raise RuntimeError("network")
    monkeypatch.setattr(anthropic, "Anthropic", boom)
    a = analyze(simulated_records(), "demo", 90)
    report, src = diagnosis.diagnose(a, api_key="sk-test")
    assert src == "rules" and "unavailable" in report


def test_normalize_graphql_node():
    node = {
        "number": 7, "title": "x", "url": "u", "createdAt": "2026-09-01T09:00:00Z", "mergedAt": "2026-09-02T09:00:00Z",
        "isDraft": False, "additions": 10, "deletions": 2, "changedFiles": 1,
        "author": {"login": "a", "__typename": "User"},
        "commits": {"nodes": [{"commit": {"authoredDate": "2026-08-31T09:00:00Z"}}]},
        "reviews": {"nodes": [
            {"state": "APPROVED", "submittedAt": "2026-09-01T20:00:00Z", "author": {"login": "b", "__typename": "User"}},
            {"state": "COMMENTED", "submittedAt": "2026-09-01T10:00:00Z", "author": {"login": "copilot", "__typename": "Bot"}},
        ]},
        "timelineItems": {"nodes": [{"createdAt": "2026-09-01T12:00:00Z"}]},
    }
    r = normalize(node)
    assert r["ready_at"].hour == 12  # draft became ready later than creation
    assert [x["author"] for x in r["reviews"]] == ["copilot", "b"]  # sorted by time
    assert r["reviews"][0]["is_bot"]
    df = pr_frame([r])
    assert df.iloc[0].first_reviewer == "b"  # bot review does not count
    assert df.iloc[0].wait_review == 8


def test_snapshot_roundtrip(tmp_path):
    recs = simulated_records()[:5]
    p = tmp_path / "s.json"
    snapshot.save(p, "x/y", 90, recs)
    back = snapshot.load(p)
    assert back["records"][0]["merged_at"] == recs[0]["merged_at"]
    assert back["records"][0]["reviews"][0]["submitted_at"] == recs[0]["reviews"][0]["submitted_at"]


def test_facts_have_no_nan():
    import json, math
    facts = facts_for_llm(analyze(simulated_records(), "demo", 90))
    txt = json.dumps(facts)
    assert "NaN" not in txt


def test_dashboard_renders():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("is-constraint" in m.value and "Waiting for review" in m.value for m in at.markdown)
    assert any("simulated team" in i.value for i in at.info)


def solo_maintainer_records():
    """Mirrors a repo like Wolverine: one maintainer self-merges nearly everything."""
    recs = [pr(i, author="maintainer", commit=0, ready=1, merged=2) for i in range(120)]
    recs += [pr(200 + i, author=f"contrib{i}", merged=40) for i in range(10)]
    recs.append(pr(999, author="contrib0", reviews=[("maintainer", "APPROVED", 5)], merged=28))
    return recs


def test_no_review_gate_is_detected_not_misdiagnosed():
    a = analyze(solo_maintainer_records(), "x/solo", 90)
    assert a.mode == "direct"
    report, _ = diagnosis.diagnose(a, api_key="")
    assert "One person's capacity" in report
    assert "Waiting to merge" not in report


def test_facts_flag_no_review_gate():
    facts = facts_for_llm(analyze(solo_maintainer_records(), "x/solo", 90))
    assert facts["review_mode"].startswith("no review gate")
    assert facts["top_author_share_pct"] > 80


def test_dashboard_renders_direct_mode(tmp_path, monkeypatch):
    import shutil
    from streamlit.testing.v1 import AppTest
    root = tmp_path / "app"
    shutil.copytree(".", root, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", "samples"))
    (root / "samples").mkdir()
    snapshot.save(root / "samples" / "x__solo.json", "x/solo", 90, solo_maintainer_records())
    at = AppTest.from_file(str(root / "app.py"), default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("No review gate" in w.value for w in at.warning)
    assert any("Who does the work" in t.label for t in at.tabs)


def test_long_tail_is_called_out():
    waits = [2, 3, 3, 4, 2, 3, 80, 3, 2, 90] * 3
    recs = [pr(i, reviews=[("r", "APPROVED", 2 + w)], merged=4 + w) for i, w in enumerate(waits)]
    report = diagnosis.rule_based_report(analyze(recs, "x/y", 90))
    assert "falling through the cracks" in report
    assert "aging alert" in report


def test_pluralization():
    assert diagnosis._fmt_h(1.0) == "1 hour"
    assert diagnosis._fmt_h(5.0) == "5 hours"
