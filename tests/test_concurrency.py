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

from helpers import HOOKS, mk_session

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
    """25 concurrent post_tool_use writers: every one exits 0 and every event is
    durable. A lost write here would silently corrupt the activity clock."""
    c = hook_env.conn()
    pk = mk_session(c, session_id="s1", cwd="/code/herd")
    n = 25
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(
            lambda _: _run(hook_env, "post_tool_use.sh", PAY, NO_THROTTLE), range(n)))
    assert [r.returncode for r in results] == [0] * n
    got = c.execute("SELECT COUNT(*) FROM events WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got == n, f"expected {n} events, got {got} — a concurrent write was lost"


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
    pk = mk_session(c, session_id="s1", cwd="/code/herd")
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
    assert c.execute("SELECT COUNT(*) FROM events WHERE session_pk=?",
                     (pk,)).fetchone()[0] == 1, "the hook gave up instead of waiting"


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
