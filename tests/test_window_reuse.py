"""M (41-43) — window reuse and the write paths. A window is a recyclable handle;
resume revives job+window with no stored flag to desync; adoption targets the LIVE
row, not a dead predecessor."""
import pytest

from helpers import W, T0, T1, T2, SOCK, mk_session, mk_herd, live_in_window, job_holder


def test_window_reuse_new_session_gets_placement(fresh):
    c = fresh()
    a = mk_session(c, pid=111, cwd="/code/herd")
    mk_herd(c, a, created_at=T0, kitty_socket=SOCK, window_id=5, source="hook")
    c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?", (T1, a))
    b = mk_session(c, pid=222, cwd="/code/herd", started_at=T2, updated_at=T2)
    mk_herd(c, b, created_at=T2, kitty_socket=SOCK, window_id=5, source="hook")
    assert live_in_window(c, SOCK, 5) == [b]


def test_db_allows_two_live_rows_in_one_window(fresh):
    """41b — the 'one live per window' invariant is app-level (reconcile's
    rebuild) now, NOT a DB constraint; the DB deliberately permits two."""
    c = fresh()
    a = mk_session(c, pid=111, cwd="/code/herd")
    mk_herd(c, a, created_at=T0, kitty_socket=SOCK, window_id=5, source="hook")
    b = mk_session(c, pid=222, cwd="/code/herd", started_at=T2, updated_at=T2)
    mk_herd(c, b, created_at=T2, kitty_socket=SOCK, window_id=5, source="hook")
    assert len(live_in_window(c, SOCK, 5)) == 2


def test_resume_revives_job_and_window_no_desync(fresh):
    """41c — the resume regression this whole model exists to fix. Old schema left
    job/window free forever after resume; the new one is self-consistent."""
    c = fresh()
    pk = mk_session(c, session_id="u1", cwd="/code/herd")
    mk_herd(c, pk, job_name="api", created_at=T0, kitty_socket=SOCK, window_id=5)
    c.execute(W["W4_end"], {"session_id": "u1", "now": T1})            # die
    assert job_holder(c, "api") is None and live_in_window(c, SOCK, 5) == []
    c.execute(W["W2b_insert"], {"session_id": "u1", "cwd": "/code/herd", "model": "opus",
                                "transcript": "/t.jsonl", "now": T2, "pid": None})   # resume
    assert job_holder(c, "api") == pk and live_in_window(c, SOCK, 5) == [pk]


@pytest.fixture
def reused_window(fresh):
    """Dead session A + live session B, both claiming window 5."""
    c = fresh()
    a = mk_session(c, pid=111, cwd="/code/herd")
    mk_herd(c, a, created_at=T0, kitty_socket=SOCK, window_id=5, source="hook")
    c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?", (T1, a))
    b = mk_session(c, pid=222, cwd="/code/herd", started_at=T2, updated_at=T2)
    mk_herd(c, b, created_at=T2, kitty_socket=SOCK, window_id=5, source="hook")
    return c, a, b


def test_w2_adopts_the_live_row(reused_window):
    c, a, b = reused_window
    c.execute(W["W2_adopt"], {"session_id": "uuid-B", "cwd": "/code/herd", "model": "opus",
                              "transcript": "/t.jsonl", "now": T2, "pid": 222, "socket": SOCK, "win": 5})
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (b,)).fetchone()[0] == "uuid-B"
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (a,)).fetchone()[0] is None


def test_w5b_adopts_the_live_row(reused_window):
    c, a, b = reused_window
    c.execute(W["W5b_adopt"], {"session_id": "uuid-C", "now": T2, "socket": SOCK, "win": 5})
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (b,)).fetchone()[0] == "uuid-C"
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (a,)).fetchone()[0] is None
