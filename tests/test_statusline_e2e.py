"""O (59-63) — statusline.sh end to end: the sink + render + fingerprint cache +
Path C adoption, driven through the real bash script."""
from helpers import T0, T1, SOCK, mk_session, mk_herd

SL_PAY = {"session_id": "s1", "model": {"id": "claude-opus-4-8"}, "session_name": "sess",
          "cwd": "/code/herd", "context_window": {"used_percentage": 42.7},
          "cost": {"total_cost_usd": 1.50},
          "rate_limits": {"five_hour": {"used_percentage": 73.5, "resets_at": 1784172774},
                          "seven_day": {"used_percentage": 12, "resets_at": 1784259174}}}


def _statusline(hook_env, payload, env=None):
    return hook_env.run("statusline.sh", payload, env)


def test_sinks_metrics_and_renders_claude_name(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s1", cwd="/code/herd")
    mk_herd(c, pk, job_name="api-refactor", created_at=T0, window_id=5)
    r = _statusline(hook_env, SL_PAY)
    row = c.execute("SELECT context_percent,total_cost_usd,rate_limit_5h_percent,"
                    "rate_limit_5h_resets_at FROM sessions WHERE session_id='s1'").fetchone()
    assert row["context_percent"] == 42 and isinstance(row["context_percent"], int)
    assert row["total_cost_usd"] == 1.5
    assert row["rate_limit_5h_percent"] == 73.5 and row["rate_limit_5h_resets_at"] == "2026-07-16T03:32:54Z"
    # ⬢ shows Claude's session_name, NOT the tier-2 job_name.
    assert "⬢ sess" in r.stdout and "api-refactor" not in r.stdout


def test_identical_tick_is_fingerprint_hit(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, SL_PAY)                               # tick 1: sink
    before = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    r2 = _statusline(hook_env, SL_PAY)                          # tick 2: cache hit
    after = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    assert before == after and r2.stdout.strip() != ""         # no write, still renders


def test_path_c_adopts_reconciled_session(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, pid=4242, cwd="/code/herd", status="unknown", status_source="reconcile")
    mk_herd(c, pk, created_at=T0, window_id=5, source="hook")
    _statusline(hook_env, {"session_id": "uuid-x", "model": {"id": "opus"}, "cwd": "/code/herd",
                           "context_window": {"used_percentage": 30}, "cost": {"total_cost_usd": 0.10}},
                {"KITTY_WINDOW_ID": "5", "KITTY_LISTEN_ON": SOCK})
    row = c.execute("SELECT session_id,context_percent FROM sessions WHERE id=?", (pk,)).fetchone()
    assert row["session_id"] == "uuid-x" and row["context_percent"] == 30


def test_tick_on_stopped_session_is_noop(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="dead", cwd="/x", status="stopped", stopped_at=T1)
    _statusline(hook_env, {"session_id": "dead", "model": {"id": "opus"}, "cwd": "/x",
                           "context_window": {"used_percentage": 99}, "cost": {"total_cost_usd": 5}})
    row = c.execute("SELECT context_percent,stopped_at FROM sessions WHERE session_id='dead'").fetchone()
    assert row["context_percent"] is None and row["stopped_at"] == T1


def test_never_moves_last_event_at(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd", last_event_at=T0, last_event_type="tool")
    _statusline(hook_env, SL_PAY)
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] == T0
