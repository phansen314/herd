"""J / K — ON DELETE CASCADE, the CHECK/FK constraints, and the DELIBERATELY
non-unique (socket, window_id) that makes window reuse work."""
import sqlite3

import pytest

from helpers import T0, T1, mk_session, mk_herd, mk_attention


def test_cascade_cleans_tier2_and_events(fresh):
    c = fresh()
    pk = mk_session(c, cwd="/a")
    mk_herd(c, pk, job_name="j", created_at=T0, kitty_socket="unix:/tmp/k1", window_id=7)
    mk_attention(c, pk, attention_at=T0)
    c.execute("INSERT INTO events(session_pk,event_type,source,timestamp) VALUES(?,'start','hook',?)", (pk, T0))
    c.execute("DELETE FROM sessions WHERE id=?", (pk,))
    orph = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE session_pk=?", (pk,)).fetchone()[0]
            for t in ("herd_sessions", "herd_attention", "events")}
    assert sum(orph.values()) == 0


@pytest.mark.parametrize("col,val", [("status", "bogus"), ("status_source", "nope")])
def test_sessions_check_rejects_garbage(fresh, col, val):
    c = fresh()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(f"INSERT INTO sessions(cwd,{col},started_at,updated_at) VALUES('/z',?,?,?)", (val, T0, T0))


def test_herd_source_check_rejects_garbage(fresh):
    c = fresh()
    pk = mk_session(c, cwd="/a")
    with pytest.raises(sqlite3.IntegrityError):
        mk_herd(c, pk, kitty_socket="s", window_id=1, source="bogus")


def test_fk_enforced_on_herd_sessions(fresh):
    c = fresh()
    with pytest.raises(sqlite3.IntegrityError):
        mk_herd(c, 999, kitty_socket="s", window_id=1)


def test_socket_window_may_repeat_across_dead_and_live(fresh):
    """(socket, window_id) is NOT unique — a dead + live pair may share a window;
    the liveness JOIN returns exactly the live occupant."""
    c = fresh()
    pk = mk_session(c, cwd="/a")
    p2 = mk_session(c, cwd="/b")
    mk_herd(c, pk, kitty_socket="unix:/tmp/k1", window_id=7)
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T1, pk))
    mk_herd(c, p2, kitty_socket="unix:/tmp/k1", window_id=7)
    live = c.execute("SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                     "WHERE h.kitty_socket='unix:/tmp/k1' AND h.window_id=7 AND s.stopped_at IS NULL").fetchall()
    assert [r["session_pk"] for r in live] == [p2]
