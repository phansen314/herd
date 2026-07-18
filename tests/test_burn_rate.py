"""The burn rate ($/h) — R_statusline, the prev_cost pair, and the awk guards.

This was the largest single hole in the suite: R_statusline had no test at all, and
the whole render path (statusline.sh's mktime/-1, c<=p and sub-cent guards) never
executed, because the shared SL_PAY fixture never produced a second tick with a
changed cost, so BURN was "" in every test that ran.

Note on clocks: the prev_cost resample CASE compares against SQLite's real
`strftime('%s','now')`, not the bound :now. Fixed test clocks (T0/T1, 2026-07-15)
are always >300s in the past, so they always take the resample branch. Tests that
need the KEEP branch therefore build stamps relative to the real clock.
"""
import datetime as dt
import json
import subprocess
import os

from helpers import W, T0, T1, HOOKS, mk_session, SL_PARAMS


def _iso(delta_s):
    """An ISO stamp `delta_s` seconds from real now — for the paths that compare
    against SQLite's / awk's live clock."""
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_s)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _sl(hook_env, payload, env=None):
    return hook_env.run("statusline.sh", payload, env)


def _pay(cost, sid="s1"):
    return {"session_id": sid, "model": {"id": "opus"}, "cwd": "/code/herd",
            "context_window": {"used_percentage": 10}, "cost": {"total_cost_usd": cost}}


# ── R_statusline itself ──────────────────────────────────────────────────────
def test_r_statusline_returns_the_prev_cost_pair(fresh):
    c = fresh()
    mk_session(c, session_id="s1")
    c.execute("UPDATE sessions SET prev_cost_usd=1.25, prev_cost_sampled_at=? "
              "WHERE session_id='s1'", (T0,))
    out = c.execute(W["R_statusline"], {"session_id": "s1"}).fetchone()[0]
    assert out == f"1.25|{T0}"


def test_r_statusline_joins_nulls_as_empty_not_null(fresh):
    """statusline.sh splits this on '|' into two bash vars; a NULL row would
    collapse the split and mis-assign PREV_AT. COALESCE('') keeps it positional."""
    c = fresh()
    mk_session(c, session_id="s1")
    assert c.execute(W["R_statusline"], {"session_id": "s1"}).fetchone()[0] == "|"


def test_r_statusline_unknown_session_returns_no_row(fresh):
    c = fresh()
    assert c.execute(W["R_statusline"], {"session_id": "ghost"}).fetchone() is None


# ── the prev_cost pair in W5 ─────────────────────────────────────────────────
def test_first_tick_captures_the_old_total_as_prev(fresh):
    """The invariant writes.sql flags as 'correct as written, don't fix it': an
    UPDATE's RHS sees the OLD row, so prev_cost_usd takes the pre-update total."""
    c = fresh()
    mk_session(c, session_id="s1")
    c.execute("UPDATE sessions SET total_cost_usd=1.0 WHERE session_id='s1'")
    c.execute(W["W5_statusline"], {**SL_PARAMS, "cost": 2.0, "now": T1, "session_id": "s1"})
    r = c.execute("SELECT total_cost_usd, prev_cost_usd FROM sessions "
                  "WHERE session_id='s1'").fetchone()
    assert (r["total_cost_usd"], r["prev_cost_usd"]) == (2.0, 1.0)


def test_fresh_sample_is_kept_not_resampled(fresh):
    """Within 300s the pair must be held, so the delta spans a useful window
    instead of collapsing to the last tick (which would read ~0 $/h)."""
    c = fresh()
    mk_session(c, session_id="s1")
    recent = _iso(-10)
    c.execute("UPDATE sessions SET total_cost_usd=1.0, prev_cost_usd=0.5, "
              "prev_cost_sampled_at=? WHERE session_id='s1'", (recent,))
    c.execute(W["W5_statusline"], {**SL_PARAMS, "cost": 2.0, "now": T1, "session_id": "s1"})
    r = c.execute("SELECT prev_cost_usd, prev_cost_sampled_at FROM sessions "
                  "WHERE session_id='s1'").fetchone()
    assert (r["prev_cost_usd"], r["prev_cost_sampled_at"]) == (0.5, recent)


def test_stale_sample_is_resampled(fresh):
    c = fresh()
    mk_session(c, session_id="s1")
    c.execute("UPDATE sessions SET total_cost_usd=1.0, prev_cost_usd=0.5, "
              "prev_cost_sampled_at=? WHERE session_id='s1'", (_iso(-600),))
    c.execute(W["W5_statusline"], {**SL_PARAMS, "cost": 2.0, "now": T1, "session_id": "s1"})
    assert c.execute("SELECT prev_cost_usd FROM sessions "
                     "WHERE session_id='s1'").fetchone()[0] == 1.0


# ── the rendered 🔥 segment, through real bash ───────────────────────────────
def _seed_for_burn(hook_env, prev_cost, prev_at, total):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    c.execute("UPDATE sessions SET total_cost_usd=?, prev_cost_usd=?, "
              "prev_cost_sampled_at=? WHERE session_id='s1'", (total, prev_cost, prev_at))
    return c


def test_burn_renders_with_the_expected_rate(hook_env):
    """$1 more spent over 60s == $60/h."""
    _seed_for_burn(hook_env, prev_cost=1.0, prev_at=_iso(-60), total=1.0)
    r = _sl(hook_env, _pay(2.0))
    assert r.returncode == 0
    assert "🔥 $60." in r.stdout, r.stdout


def test_burn_hidden_when_cost_did_not_increase(hook_env):
    """The c<=p guard: a flat tick must not print '$0.00/h'."""
    _seed_for_burn(hook_env, prev_cost=2.0, prev_at=_iso(-60), total=2.0)
    r = _sl(hook_env, _pay(2.0))
    assert r.returncode == 0 and "🔥" not in r.stdout


def test_burn_hidden_when_rate_is_sub_cent(hook_env):
    """Noise suppression: <$0.01/h is hidden rather than shown as 0."""
    _seed_for_burn(hook_env, prev_cost=1.0, prev_at=_iso(-3600), total=1.0)
    r = _sl(hook_env, _pay(1.000001))
    assert r.returncode == 0 and "🔥" not in r.stdout


def test_burn_survives_an_unparseable_prev_stamp(hook_env):
    """mktime() returns -1 on garbage; the a<=0/b<=0 guard must swallow it rather
    than emit a bogus rate or fail the hook."""
    _seed_for_burn(hook_env, prev_cost=1.0, prev_at="not-a-timestamp", total=1.0)
    r = _sl(hook_env, _pay(2.0))
    assert r.returncode == 0 and "🔥" not in r.stdout


def test_burn_absent_on_the_first_ever_tick(hook_env):
    """No prior sample -> nothing to diff against -> no 🔥, and no crash."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    r = _sl(hook_env, _pay(1.0))
    assert r.returncode == 0 and "🔥" not in r.stdout
