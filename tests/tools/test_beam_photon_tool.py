"""beam_photon tool (keeper ruling 2026-09-24, task 633)."""

import json
import sqlite3

import pytest

from tools import beam_photon_tool as bpt


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    path = tmp_path / "memory_store.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE facts (fact_id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'general', tags TEXT DEFAULT '', trust_score REAL DEFAULT 0.5);
        CREATE VIRTUAL TABLE facts_fts USING fts5(content, tags);
        CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
        END;
        """
    )
    conn.executemany(
        "INSERT INTO facts (content, category, trust_score) VALUES (?, ?, ?)",
        [
            ("Mailroom drafts are staged; the keeper sends them himself.", "user_pref", 0.8),
            ("Mailroom once sent a draft early; distrusted.", "general", 0.1),
            ("Printer lives on the office shelf.", "general", 0.6),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(bpt, "_memory_db_path", lambda: path)
    return path


def _call(args, session_id="s1"):
    return json.loads(bpt.beam_photon(args, session_id=session_id))


def test_search_memory_is_ranked_and_trust_filtered(memory_db):
    result = _call({"action": "search_memory", "query": "mailroom drafts keeper"})
    assert result["success"]
    assert [m["fact_id"] for m in result["matches"]] == [1]
    assert result["matches"][0]["trust_score"] == 0.8


def test_search_memory_opens_read_only(memory_db):
    with bpt._connect_memory_ro() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO facts (content) VALUES ('x')")


def test_record_requires_memory_search_first(memory_db):
    result = _call({"action": "record", "title": "t", "text": "x"})
    assert result["success"] is False and "search_memory" in result["error"]


def test_record_rejects_unknown_memory_photon(memory_db):
    result = _call({"action": "record", "title": "t", "text": "x", "memory_queries": ["q"], "memory_fact_ids": [99]})
    assert result["success"] is False and "99" in result["error"]


def _fake_service(route_payload, calls):
    def run(args):
        calls.append(args)
        if args[0] == "beam-photon-route":
            return route_payload
        entry = json.loads(args[args.index("--entry") + 1])
        return {
            "ok": True,
            "photon_id": "P-2026-09-24-001",
            "photon_uid": "photon:atrium:x:P-2026-09-24-001",
            "ledger": (entry.get("beam") or "23_metacognition/photon_ledgers") + "/l.jsonl",
            "beam": entry.get("beam"),
            "created_ledger": False,
            "commit": {"committed": True, "commit": "abc1234"},
        }
    return run


def test_record_heuristic_route_goes_to_backstop_with_candidates(memory_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bpt, "_run_service", _fake_service({
        "ok": True, "basis": "beam_path",
        "recommended": {"beam": "20_digital_architecture/oasis_mailroom"},
        "candidates": [{"beam": "13_fleet"}],
    }, calls))
    result = _call({
        "action": "record", "title": "Drafts wait for the keeper", "text": "Stage drafts; he sends.",
        "skill": "oasis-mailroom-operations", "memory_queries": ["mailroom drafts"], "memory_fact_ids": [1],
    })
    assert result["success"] and result["committed"] and result["memory_fact_ids"] == [1]
    assert calls[0][:1] == ["beam-photon-route"] and "--skill" in calls[0]
    append = calls[1]
    assert append[0] == "beam-photon-append" and "--commit" in append
    entry = json.loads(append[append.index("--entry") + 1])
    assert "beam" not in entry
    assert entry["candidate_homes"] == ["20_digital_architecture/oasis_mailroom", "13_fleet"]
    assert entry["memory_corroboration"] == "found"
    assert entry["memory_photons"][0]["content_sha256"].startswith("sha256:")
    assert entry["memory_queries"] == ["mailroom drafts"]
    assert entry["related_skill"]["name"] == "oasis-mailroom-operations"
    assert entry["source_refs"][0]["note"] == "Hermes session s1"


def test_record_follows_skill_history_and_explicit_beam(memory_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bpt, "_run_service", _fake_service({
        "ok": True, "basis": "skill_history", "recommended": {"beam": "20_digital_architecture/oasis_mailroom"},
    }, calls))
    result = _call({"action": "record", "title": "t", "text": "x", "skill": "oasis-mailroom-operations", "memory_queries": ["q"]})
    assert result["beam"] == "20_digital_architecture/oasis_mailroom"
    assert result["memory_corroboration"] == "none_found"

    calls.clear()
    result = _call({"action": "record", "title": "t", "text": "x", "beam": "13_fleet", "memory_queries": ["q"]})
    assert [c[0] for c in calls] == ["beam-photon-append"]
    assert result["beam"] == "13_fleet"


def test_unknown_action():
    assert "error" in json.loads(bpt.beam_photon({"action": "nope"}))
