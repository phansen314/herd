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


# ── /clear: Claude's, not herd's — a new session_id in the SAME window ────────
def _clear_to(c, session_id, pid, now, win=5):
    """What SessionEnd + SessionStart actually do on /clear: the old session stops,
    a NEW session_id starts in the same window, on the SAME process."""
    c.execute(W["W2b_insert"], {"session_id": session_id, "cwd": "/code/herd",
                                "model": "opus", "transcript": "/t", "pid": pid, "now": now})
    c.execute(W["W2b_placement"], {"session_id": session_id, "job": None, "socket": SOCK,
                                   "win": win, "now": now})


def _spawned_job(c, job="api", pid=1830578, win=5):
    pk = c.execute(W["W1_spawn_session"], {"cwd": "/code/herd", "now": T0}).lastrowid
    c.execute(W["W1_spawn_herd"], {"pk": pk, "job": job, "now": T0, "socket": SOCK})
    c.execute(W["W1_spawn_window"], {"pk": pk, "win": win, "now": T0})
    c.execute(W["W2_adopt"], {"session_id": "uuid-A", "cwd": "/code/herd", "model": "opus",
                              "transcript": "/t", "pid": pid, "now": T0,
                              "socket": SOCK, "win": win})
    return pk


def test_clear_carries_the_job_name_to_the_new_session(fresh):
    """W2_adopt cannot match on /clear — its subquery needs stopped_at IS NULL and
    the predecessor was just stopped — so a spawned job fell through to
    W2b_placement, which omitted job_name. `herd jump <job>` broke permanently on
    the first /clear."""
    c = fresh()
    _spawned_job(c)
    c.execute(W["W4_end"], {"session_id": "uuid-A", "now": T1})
    _clear_to(c, "uuid-B", 1830578, T1)
    assert job_holder(c, "api") is not None, "job name lost on /clear"
    assert len(c.execute(W["R_job_live"], {"job": "api"}).fetchall()) == 1


def test_the_job_name_survives_repeated_clears(fresh):
    """Real windows chain several — one live herd showed four in a row sharing one
    pid — so inheritance has to come from the immediate predecessor each time."""
    c = fresh()
    _spawned_job(c)
    prev, stamps = "uuid-A", [T1, T2, "2026-07-15T10:15:00.000Z"]
    for i, now in enumerate(stamps):
        c.execute(W["W4_end"], {"session_id": prev, "now": now})
        prev = f"uuid-{i}"
        _clear_to(c, prev, 1830578, now)
        assert job_holder(c, "api") is not None, f"lost after clear #{i + 1}"


def test_an_unrelated_claude_in_a_recycled_window_inherits_nothing(fresh):
    """THE reason the discriminator is the pid and not a time window: /clear keeps
    the process, a genuinely new claude does not."""
    c = fresh()
    _spawned_job(c)
    c.execute(W["W4_end"], {"session_id": "uuid-A", "now": T1})
    _clear_to(c, "uuid-Z", 9999999, T2)           # different process
    assert job_holder(c, "api") is None


def test_a_pidless_session_inherits_nothing(fresh):
    """claude_pid() finding no ancestor yields a NULL pid, which must match nothing
    rather than everything."""
    c = fresh()
    _spawned_job(c)
    c.execute(W["W4_end"], {"session_id": "uuid-A", "now": T1})
    _clear_to(c, "uuid-N", None, T2)
    assert job_holder(c, "api") is None


def test_inheritance_does_not_mutate_an_existing_job_name(fresh):
    """The mutability contract still holds: job_name is immutable ONCE SET, so the
    ON CONFLICT branch must not touch it. Only the INSERT branch inherits."""
    c = fresh()
    pk = mk_session(c, session_id="u1", pid=555, cwd="/code/herd")
    mk_herd(c, pk, job_name="mine", created_at=T0, kitty_socket=SOCK, window_id=5)
    dead = mk_session(c, session_id="u0", pid=555, cwd="/code/herd", stopped_at=T0)
    mk_herd(c, dead, job_name="theirs", created_at=T0, kitty_socket=SOCK, window_id=5)
    c.execute(W["W2b_placement"], {"session_id": "u1", "job": None, "socket": SOCK, "win": 5, "now": T2})
    assert c.execute("SELECT job_name FROM herd_sessions WHERE session_pk=?",
                     (pk,)).fetchone()[0] == "mine"


# ── adoption must be deterministic when a window holds two live rows ──────────
def test_adoption_is_deterministic_with_two_live_rows_in_a_window(fresh):
    """The DB deliberately permits two live rows per window and the subquery was
    bare, so which one adoption targeted was a query-plan detail rather than a
    stated rule. ASC pins it to the oldest."""
    c = fresh()
    old = mk_session(c, session_id=None, pid=None, cwd="/x", status="unknown")
    mk_herd(c, old, job_name="first", kitty_socket=SOCK, window_id=5)
    new = mk_session(c, session_id=None, pid=None, cwd="/x", status="unknown")
    mk_herd(c, new, job_name="second", kitty_socket=SOCK, window_id=5)
    c.execute(W["W2_adopt"], {"session_id": "uuid-X", "cwd": "/x", "model": "opus",
                              "transcript": "/t", "pid": 333, "now": T1,
                              "socket": SOCK, "win": 5})
    got = c.execute("SELECT id FROM sessions WHERE session_id='uuid-X'").fetchone()[0]
    assert got == old


def test_adoption_declines_rather_than_stamping_a_taken_session_id(fresh):
    """WHY ASC AND NOT DESC — the opposite of what it looks like it should be.

    Preferring the NEWEST row sounds right (the older is the likely stale one) and
    is actively harmful. With an adopted session plus a NEWER unadopted reservation
    in one window, DESC returns the reservation, the outer `session_id IS NULL`
    passes, and a SessionStart re-fire stamps an already-taken session_id onto it:
    UNIQUE constraint failed: sessions.session_id.

    ASC returns the adopted row, the outer predicate declines it, and the hook falls
    through to W2b_insert — which is the correct handling of a re-fire."""
    c = fresh()
    adopted = mk_session(c, session_id="uuid-X", pid=111, cwd="/x")
    mk_herd(c, adopted, kitty_socket=SOCK, window_id=5)
    reservation = mk_session(c, session_id=None, pid=None, cwd="/x", status="unknown")
    mk_herd(c, reservation, job_name="api", kitty_socket=SOCK, window_id=5)
    assert reservation > adopted                     # the trap needs this ordering
    n = c.execute(W["W2_adopt"], {"session_id": "uuid-X", "cwd": "/x", "model": "opus",
                                  "transcript": "/t", "pid": 111, "now": T1,
                                  "socket": SOCK, "win": 5}).rowcount
    assert n == 0                                    # declined, no IntegrityError
    assert c.execute("SELECT session_id FROM sessions WHERE id=?",
                     (reservation,)).fetchone()[0] is None


def test_path_c_adoption_is_ordered_the_same_way(fresh):
    """W5b_adopt carries the identical subquery, so it gets the identical rule —
    the two adoption routes must not disagree about which row a window means."""
    c = fresh()
    old = mk_session(c, session_id=None, pid=None, cwd="/x", status="unknown")
    mk_herd(c, old, job_name="first", kitty_socket=SOCK, window_id=5)
    new = mk_session(c, session_id=None, pid=None, cwd="/x", status="unknown")
    mk_herd(c, new, job_name="second", kitty_socket=SOCK, window_id=5)
    c.execute(W["W5b_adopt"], {"session_id": "uuid-Y", "now": T1, "socket": SOCK, "win": 5})
    got = c.execute("SELECT id FROM sessions WHERE session_id='uuid-Y'").fetchone()[0]
    assert got == old
