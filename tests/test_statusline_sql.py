"""H / 58 — W5_statusline is UPDATE-only (never creates/resurrects) and captures
rate limits with epoch->ISO conversion, keeping prior values on an absent tick."""
from helpers import W, T0, T1, T2, mk_session

_SL = {"model": None, "sname": None, "ctx": 50, "cost": None, "branch": None,
       "rl5": None, "rl5reset": None, "rl7": None, "rl7reset": None}


def test_statusline_on_unknown_session_is_noop(fresh):
    c = fresh()
    assert c.execute(W["W5_statusline"], {**_SL, "now": T1, "session_id": "ghost"}).rowcount == 0


def test_statusline_cannot_resurrect_stopped(fresh):
    c = fresh()
    mk_session(c, session_id="dead", status="stopped", stopped_at=T1)
    assert c.execute(W["W5_statusline"], {**_SL, "now": T2, "session_id": "dead"}).rowcount == 0


def test_rate_limits_epoch_to_iso_and_coalesce(fresh):
    c = fresh()
    mk_session(c, session_id="s1")
    c.execute(W["W5_statusline"], {"model": None, "sname": None, "ctx": None, "cost": None,
                                   "branch": None, "rl5": "73.5", "rl5reset": "1784172774",
                                   "rl7": "12", "rl7reset": "1784259174",
                                   "now": T1, "session_id": "s1"})
    r = c.execute("SELECT rate_limit_5h_percent,rate_limit_5h_resets_at FROM sessions "
                  "WHERE session_id='s1'").fetchone()
    assert r["rate_limit_5h_percent"] == 73.5
    assert r["rate_limit_5h_resets_at"] == "2026-07-16T03:32:54Z"    # epoch converted
    # a later tick with no rate limits must NOT wipe them (COALESCE keeps prior)
    c.execute(W["W5_statusline"], {**_SL, "ctx": None, "now": T2, "session_id": "s1"})
    assert c.execute("SELECT rate_limit_5h_percent FROM sessions WHERE session_id='s1'").fetchone()[0] == 73.5
