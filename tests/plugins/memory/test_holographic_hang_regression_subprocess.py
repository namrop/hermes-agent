"""Isolated-process hang regression for the oversized-prefetch / shared-SQLite
gateway freeze (2026-09-17, recurrence 2026-09-18).

The child process rebuilds the incident shape against a disposable database:

1. manager A prefetches a synthetic multi-megabyte packet with hundreds of
   thousands of distinct significant words (the Nightly Lantern packet shape);
2. while that runs, manager B on the same database path performs an ordinary
   prefetch, a system-prompt fact count and a fact write — the "next Discord
   turn" that previously entered the same shared connection and froze the
   interpreter;
3. an independent witness thread keeps ticking throughout.

The parent enforces a hard wall-clock deadline (``subprocess.run(timeout=…)``)
and ``faulthandler`` dumps every thread if the child stalls, so a regression
shows up as a killed child with stacks, never as a hung test runner. Only
pre-existing public API is used in the child so the test is meaningful on
both the broken and the repaired tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")

_REPO_ROOT = Path(__file__).resolve().parents[3]

_CHILD = r'''
import faulthandler, json, os, sys, threading, time
faulthandler.enable()
faulthandler.dump_traceback_later(35, exit=True)

import agent.memory_manager as mm_mod
assert mm_mod.__file__.startswith(os.getcwd()), mm_mod.__file__

from agent.memory_manager import MemoryManager
from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore

db = os.path.join(os.environ["HERMES_HOME"], "memory_store.db")
store = MemoryStore(db, hrr_dim=64)
store.add_fact("The Thursday deployment rollback failed because of stale migration state.", category="project")
rows = []
for i in range(3000):
    words = " ".join(f"w{(i * 37 + k * 101) % 400000}x" for k in range(30))
    rows.append((f"bulk {i} deploy rollback {words}", "project", "bulk", 0.6))
with store._lock:
    store._conn.executemany("INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)", rows)
    store._conn.commit()
print("seeded", flush=True)

packet = "## Script Output\n" + " ".join(f"w{i}x" for i in range(400000)) + "\nReflect on the day."
summary = {"packet_chars": len(packet)}
t0 = time.monotonic()
summary["fts_terms"] = FactRetriever._sanitize_fts_query(packet).count(" OR ") + 1
summary["sanitize_s"] = round(time.monotonic() - t0, 3)
print("sanitized", summary["fts_terms"], flush=True)

prov_a = HolographicMemoryProvider(config={"db_path": db, "hrr_dim": 64}); prov_a.initialize("a")
prov_b = HolographicMemoryProvider(config={"db_path": db, "hrr_dim": 64}); prov_b.initialize("b")
mgr_a = MemoryManager(); mgr_a.add_provider(prov_a)
mgr_b = MemoryManager(); mgr_b.add_provider(prov_b)

ticks = []
stop = threading.Event()
def witness():
    while not stop.is_set():
        ticks.append(time.monotonic()); time.sleep(0.05)
threading.Thread(target=witness, daemon=True).start()

a_box = {}
def run_a():
    t = time.monotonic()
    a_box["result"] = mgr_a.prefetch_all(packet)
    a_box["elapsed"] = round(time.monotonic() - t, 3)
ta = threading.Thread(target=run_a, daemon=True); ta.start()
time.sleep(0.5)
print("second statement on the same database", flush=True)
t1 = time.monotonic()
b_text = mgr_b.prefetch_all("what happened with the deployment rollback")
b_count = prov_b.system_prompt_block()
fid = store.add_fact("written while the packet prefetch ran", category="tool")
summary["b_elapsed_s"] = round(time.monotonic() - t1, 3)
summary["b_hit"] = "deployment rollback" in b_text.lower()
summary["b_count_ok"] = "facts stored" in b_count
summary["write_ok"] = fid > 0
ta.join(20)
summary["a_alive_after_join"] = ta.is_alive()
summary["a_elapsed_s"] = a_box.get("elapsed")
stop.set()
summary["witness_ticks"] = len(ticks)
gaps = [b - a for a, b in zip(ticks, ticks[1:])]
summary["witness_max_gap_s"] = round(max(gaps), 3) if gaps else None
prov_a.shutdown(); prov_b.shutdown(); store.close()
print("SUMMARY " + json.dumps(summary), flush=True)
'''


def test_oversized_packet_prefetch_cannot_stall_a_sibling_turn(tmp_path):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    env["PYTHONPATH"] = str(_REPO_ROOT)

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, f"child failed/stalled (rc={proc.returncode})"

    summary_line = next(line for line in proc.stdout.splitlines() if line.startswith("SUMMARY "))
    summary = json.loads(summary_line[len("SUMMARY "):])

    assert summary["packet_chars"] > 3_000_000
    assert summary["fts_terms"] <= 64, summary
    assert summary["sanitize_s"] < 2.0, summary
    assert summary["b_hit"] is True, summary
    assert summary["b_count_ok"] is True, summary
    assert summary["write_ok"] is True, summary
    assert summary["b_elapsed_s"] < 10.0, summary
    assert summary["a_alive_after_join"] is False, summary
    assert summary["a_elapsed_s"] is not None and summary["a_elapsed_s"] < 10.0, summary
    assert summary["witness_max_gap_s"] is not None and summary["witness_max_gap_s"] < 2.0, summary
