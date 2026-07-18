"""E — job names are recyclable handles via R_job_live + sessions.stopped_at, with
no trigger and no UNIQUE index. Death frees the name; history is retained."""
from helpers import T0, T2, mk_session, mk_herd, job_holder


def test_r_job_live_reports_live_holder(fresh):
    c = fresh()
    p1 = mk_session(c, cwd="/a")
    mk_herd(c, p1, job_name="api-refactor", created_at=T0, kitty_socket="unix:/tmp/k1", window_id=7)
    assert job_holder(c, "api-refactor") == p1


def test_death_frees_name_with_no_trigger(fresh):
    c = fresh()
    p1 = mk_session(c, cwd="/a")
    mk_herd(c, p1, job_name="api-refactor", created_at=T0, kitty_socket="unix:/tmp/k1", window_id=7)
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T2, p1))
    assert job_holder(c, "api-refactor") is None


def test_name_reusable_after_death_history_retained(fresh):
    c = fresh()
    p1 = mk_session(c, cwd="/a")
    mk_herd(c, p1, job_name="api-refactor", created_at=T0, kitty_socket="unix:/tmp/k1", window_id=7)
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T2, p1))
    p2 = mk_session(c, cwd="/b")
    mk_herd(c, p2, job_name="api-refactor", created_at=T2, kitty_socket="unix:/tmp/k1", window_id=8)
    assert c.execute("SELECT COUNT(*) FROM herd_sessions WHERE job_name='api-refactor'").fetchone()[0] == 2
    assert job_holder(c, "api-refactor") == p2   # JOIN picks the live one


def test_null_job_names_never_held(fresh):
    c = fresh()
    for cwd, win in (("/c", 20), ("/d", 21)):
        p = mk_session(c, cwd=cwd)
        mk_herd(c, p, kitty_socket="unix:/tmp/k1", window_id=win, source="hook")
    assert job_holder(c, None) is None
