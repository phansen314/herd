"""G / 42 — the idle-signal thesis: statusline moves updated_at but never
last_event_at, and lifecycle writes advance last_event_at on repeated same-status
events (no clock-freezing guard)."""
from helpers import W, T0, T1, T2, mk_session

_SL = {"model": None, "sname": None, "ctx": 42, "cost": 1.5, "branch": None,
       "rl5": None, "rl5reset": None, "rl7": None, "rl7reset": None}


def test_statusline_moves_updated_not_last_event(fresh):
    c = fresh()
    mk_session(c, session_id="s1", last_event_at=T0, last_event_type="tool")
    for t in (T1, T2):
        c.execute(W["W5_statusline"], {**_SL, "now": t, "session_id": "s1"})
    r = c.execute("SELECT last_event_at,updated_at FROM sessions WHERE session_id='s1'").fetchone()
    assert r["updated_at"] == T2                 # any write moves it
    assert r["last_event_at"] == T0              # the gap IS the attention signal
    assert r["last_event_at"] != r["updated_at"]


def test_last_event_advances_on_repeated_same_status(fresh):
    """42 — post_tool_use always sends 'working'; a status-change guard would
    freeze last_event_at and page you about a busy session."""
    c = fresh()
    mk_session(c, session_id="s1", last_event_at=T0, last_event_type="tool")
    for t in ("2026-07-15T10:01:00.000Z", "2026-07-15T10:02:00.000Z", T1):
        c.execute(W["W4_event"], {"status": "working", "now": t, "etype": "tool", "session_id": "s1"})
        c.execute(W["W4_event_log"], {"etype": "tool", "now": t, "raw": None, "session_id": "s1"})
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] == T1
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3   # events + sessions agree
