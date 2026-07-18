"""Concurrent writers — the scenario busy_timeout, WAL and BEGIN IMMEDIATE exist
for, and which nothing exercised. Before this file you could delete
`-cmd ".timeout 3000"` from common.sh's db() and the suite stayed green.

Real shape of the contention: N sessions each firing hooks ~per tool call, plus a
statusline per session ~1/sec, plus the daemon's reaper tick — all separate
processes against one file.
"""
import json
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from herd.spawn import SpawnSpec, spawn

from helpers import HOOKS, T0, mk_session

PAY = {"session_id": "s1", "cwd": "/code/herd", "model": {"id": "opus"}}
NO_THROTTLE = {"HERD_TOOL_THROTTLE": "0"}   # else the 2s coalesce hides the race


def _run(hook_env, script, payload, env=None):
    e = dict(os.environ, HERD_DB=hook_env.path, HERD_RUNTIME=hook_env.runtime,
             HERD_ERRLOG=f"{hook_env.runtime}/err.log")
    if env:
        e.update(env)
    return subprocess.run(["bash", str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def test_parallel_hot_path_hooks_all_land(hook_env):
    """25 concurrent post_tool_use writers, one per DISTINCT session: every one
    exits 0 and every write is durable. Distinct sessions (not one) so a lost write
    is countable — 25 UPDATEs to a single row collapse and hide a loss; they still
    serialize on the one DB write lock, so busy_timeout/WAL stay under test."""
    c = hook_env.conn()
    n = 25
    for i in range(n):
        mk_session(c, session_id=f"s{i}", cwd="/code/herd", last_event_at=T0)
    pay = lambda i: {"session_id": f"s{i}", "tool_name": "Bash", "tool_input": {},
                     "tool_response": "ok", "hook_event_name": "PostToolUse"}
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(
            lambda i: _run(hook_env, "post_tool_use.sh", pay(i), NO_THROTTLE), range(n)))
    assert [r.returncode for r in results] == [0] * n
    landed = c.execute("SELECT COUNT(*) FROM sessions WHERE last_event_at IS NOT ?",
                       (T0,)).fetchone()[0]
    assert landed == n, f"only {landed}/{n} writes landed — a concurrent write was lost"


def test_parallel_mixed_hooks_do_not_corrupt_status(hook_env):
    """Different hooks racing on one session. The last writer wins; what must NOT
    happen is a nonzero exit or a status outside the CHECK set."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    jobs = [("post_tool_use.sh", PAY), ("stop.sh", PAY), ("notification.sh", PAY)] * 8
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda j: _run(hook_env, j[0], j[1], NO_THROTTLE), jobs))
    assert all(r.returncode == 0 for r in results)
    status = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    assert status in ("working", "waiting", "needs_approval")


def test_hook_waits_out_a_held_write_lock(hook_env):
    """busy_timeout=3000 in db(): a hook must WAIT for a lock held briefly (the
    daemon mid-tick), not fail. Held ~0.5s, well inside the timeout."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd", last_event_at=T0)
    released = threading.Event()
    holding = threading.Event()

    def hold():
        # the connection must be created in the thread that uses it
        h = sqlite3.connect(hook_env.path, isolation_level=None)
        h.execute("BEGIN IMMEDIATE")
        h.execute("UPDATE sessions SET cwd='/held' WHERE session_id='s1'")
        holding.set()
        time.sleep(0.5)
        h.execute("COMMIT")
        h.close()
        released.set()

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(5), "lock was never acquired"
    r = _run(hook_env, "post_tool_use.sh", PAY, NO_THROTTLE)
    t.join()
    assert released.is_set()
    assert r.returncode == 0
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] != T0, \
        "the hook gave up instead of waiting"


def test_hook_exits_zero_even_when_the_lock_outlasts_the_timeout(hook_env):
    """The other side of the contract: when waiting is not enough, the hook still
    must not block Claude. Losing the write is acceptable; a nonzero exit is not.

    Costs ~3s of wall clock: db()'s `.timeout 3000` is hardcoded in common.sh, so
    there is no knob to shorten it from here."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    holder = sqlite3.connect(hook_env.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE sessions SET cwd='/held' WHERE session_id='s1'")
    try:
        r = _run(hook_env, "post_tool_use.sh", PAY, NO_THROTTLE)
        assert r.returncode == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_concurrent_statusline_and_hook(hook_env):
    """The realistic pair: a statusline sinking metrics while lifecycle hooks fire.
    They touch the same row from different processes."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    sl_pay = {**PAY, "context_window": {"used_percentage": 42},
              "cost": {"total_cost_usd": 1.0}}
    jobs = ([("statusline.sh", sl_pay)] * 10) + ([("post_tool_use.sh", PAY)] * 10)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda j: _run(hook_env, j[0], j[1], NO_THROTTLE), jobs))
    assert all(r.returncode == 0 for r in results)
    r = c.execute("SELECT status, context_percent FROM sessions "
                  "WHERE session_id='s1'").fetchone()
    # both writers' effects survive: the statusline never clobbers status, the
    # lifecycle hook never clobbers metrics.
    assert r["status"] == "working" and r["context_percent"] == 42


# ── spawn(): the reserve-before-launch race ──────────────────────────────────
def _spawn_conn(path):
    """A connection shaped like the CLI's: autocommit, so spawn() drives BEGIN
    IMMEDIATE itself. Per-thread — sqlite objects are not shareable across threads."""
    c = sqlite3.connect(str(path), timeout=5)
    c.isolation_level = None
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def test_concurrent_spawns_of_one_job_name_only_one_wins(fresh, tmp_path):
    """The TOCTOU: the live-job check used to sit outside the transaction, with a
    kitty launch (subprocess + socket round trip) between it and the INSERT. Two
    spawners both passed the check and both inserted, leaving two live sessions
    holding one job name — and no unique index can catch it, because job_name must
    repeat across dead rows.

    Fails against check -> launch -> insert; passes with reserve -> launch -> stamp.
    """
    fresh(name="race.db").close()               # create + apply schema, then reopen
    path = tmp_path / "race.db"
    barrier = threading.Barrier(2)

    def slow_launch(spec, socket):
        """Stands in for `kitten @ launch` — the real gap is I/O of this order."""
        time.sleep(0.15)
        return 99

    def attempt(_):
        conn = _spawn_conn(path)
        try:
            spec = SpawnSpec(job="api", cwd="/code/herd")
            barrier.wait(timeout=5)             # maximise the overlap
            return spawn(conn, spec, "unix:/tmp/kitty-1", T0, launch_fn=slow_launch)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt, range(2)))

    won = [r for r in results if r[0]]
    lost = [r for r in results if not r[0]]
    assert len(won) == 1, f"expected exactly one winner, got {results}"
    assert "already holds the job" in lost[0][1]

    c = _spawn_conn(path)
    live = c.execute(
        "SELECT COUNT(*) FROM herd_sessions h JOIN sessions s ON s.id = h.session_pk "
        "WHERE h.job_name='api' AND s.stopped_at IS NULL").fetchone()[0]
    c.close()
    assert live == 1, f"{live} live sessions hold 'api' — the handle is ambiguous"


def test_failed_launch_frees_the_reserved_name_immediately(fresh, tmp_path):
    """Reserving before launching means a failed launch must clean up, or the name
    stays taken by a session that never existed."""
    fresh(name="abort.db").close()
    path = tmp_path / "abort.db"
    conn = _spawn_conn(path)
    ok, msg, _ = spawn(conn, SpawnSpec(job="api", cwd="/x"), "unix:/tmp/kitty-1", T0,
                       launch_fn=lambda s, k: None)
    assert not ok and "kitty launch failed" in msg
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    ok2, _, _ = spawn(conn, SpawnSpec(job="api", cwd="/x"), "unix:/tmp/kitty-1", T0,
                      launch_fn=lambda s, k: 7)
    assert ok2, "the name was still held after a failed launch"
    conn.close()


def test_reservation_is_not_visible_as_a_placement_until_launched(fresh, tmp_path):
    """Between reserve and stamp, window_id is NULL. focus.py already treats that as
    'no window to focus yet', so the intermediate state is safe to observe."""
    fresh(name="reserve.db").close()
    path = tmp_path / "reserve.db"
    conn = _spawn_conn(path)
    seen = {}

    def peek_launch(spec, socket):
        c2 = _spawn_conn(path)                  # a DIFFERENT connection, mid-spawn
        seen["row"] = c2.execute(
            "SELECT job_name, window_id FROM herd_sessions").fetchone()
        c2.close()
        return 42

    spawn(conn, SpawnSpec(job="api", cwd="/x"), "unix:/tmp/kitty-1", T0,
          launch_fn=peek_launch)
    assert seen["row"]["job_name"] == "api"     # committed: that is what blocks a racer
    assert seen["row"]["window_id"] is None     # but not yet placed
    assert conn.execute("SELECT window_id FROM herd_sessions").fetchone()[0] == 42
    conn.close()
