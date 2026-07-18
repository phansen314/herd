"""F / 67 / 68* — the pid unique-liveness invariant, AUTOINCREMENT durability,
and W2c_pid_claim's steal-before-stamp collision safety."""
import sqlite3

import pytest

from helpers import W, T0, T1, T2, mk_session, mk_herd


def test_many_dead_sessions_share_a_pid(fresh):
    c = fresh()
    for _ in range(2):
        mk_session(c, pid=4821, stopped_at=T1)
    mk_session(c, pid=4821)   # one live
    # no raise == pass


def test_only_one_live_session_per_pid(fresh):
    c = fresh()
    mk_session(c, pid=4821)
    with pytest.raises(sqlite3.IntegrityError):
        mk_session(c, pid=4821, cwd="/b")


def test_many_live_null_pid_coexist(fresh):
    c = fresh()
    mk_session(c, cwd="/x")
    mk_session(c, cwd="/y")   # NULL pid, both live — no unique conflict


# ── 67. AUTOINCREMENT: a deleted id is never recycled ────────────────────────
def test_id_never_recycled_after_delete(fresh):
    c = fresh()
    i1 = mk_session(c, cwd="/a")
    c.execute("DELETE FROM sessions WHERE id=?", (i1,))
    i2 = mk_session(c, cwd="/b", started_at=T2, updated_at=T2)
    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='sessions'").fetchone()
    assert i2 > i1 and seq is not None


# ── 68. resume sets the FRESH walked pid ─────────────────────────────────────
def test_w2b_revive_sets_fresh_pid(fresh):
    c = fresh()
    mk_session(c, session_id="u9", pid=111, cwd="/code/herd")
    c.execute(W["W4_end"], {"session_id": "u9", "now": T1})
    c.execute(W["W2b_insert"], {"session_id": "u9", "cwd": "/code/herd", "model": "opus",
                                "transcript": "/t.jsonl", "now": T2, "pid": 999})
    assert c.execute("SELECT pid FROM sessions WHERE session_id='u9'").fetchone()["pid"] == 999


def test_w2_adopt_stamps_walked_pid(fresh):
    c = fresh()
    pk = mk_session(c, cwd="/code/app", status="unknown", status_source="reconcile")
    mk_herd(c, pk, job_name="api", created_at=T0, kitty_socket="unix:/tmp/kitty-1",
            window_id=7, herd_var="api")
    c.execute(W["W2_adopt"], {"session_id": "a1", "cwd": "/code/app", "model": "opus",
                              "transcript": "/t.jsonl", "now": T1,
                              "socket": "unix:/tmp/kitty-1", "win": 7, "pid": 555})
    assert c.execute("SELECT pid FROM sessions WHERE session_id='a1'").fetchone()["pid"] == 555


def test_w2b_insert_stamps_walked_pid(fresh):
    c = fresh()
    c.execute(W["W2b_insert"], {"session_id": "b1", "cwd": "/x", "model": "opus",
                                "transcript": "/t.jsonl", "now": T0, "pid": 555})
    assert c.execute("SELECT pid FROM sessions WHERE session_id='b1'").fetchone()["pid"] == 555


# ── 68c. W2c_pid_claim reaps the stale holder, spares the claimant ───────────
def test_pid_claim_reaps_stale_holder(fresh):
    c = fresh()
    a = mk_session(c, session_id="old", pid=777)
    c.execute(W["W2c_pid_claim"], {"pid": 777, "session_id": "new", "now": T1})
    r = c.execute("SELECT status,status_source,stopped_at FROM sessions WHERE id=?", (a,)).fetchone()
    assert (r["stopped_at"], r["status"], r["status_source"]) == (T1, "stopped", "pid")


def test_pid_claim_spares_the_claimant(fresh):
    c = fresh()
    s = mk_session(c, session_id="me", pid=888)
    c.execute(W["W2c_pid_claim"], {"pid": 888, "session_id": "me", "now": T1})
    assert c.execute("SELECT stopped_at FROM sessions WHERE id=?", (s,)).fetchone()["stopped_at"] is None


def test_claim_plus_insert_is_collision_safe(fresh):
    """68d — the money check: claim frees the ghost so W2b_insert's pid write does
    not trip idx_sessions_pid_live."""
    c = fresh()
    old = mk_session(c, session_id="ghost", pid=777)
    c.execute(W["W2c_pid_claim"], {"pid": 777, "session_id": "fresh", "now": T1})
    c.execute(W["W2b_insert"], {"session_id": "fresh", "cwd": "/b", "model": "opus",
                                "transcript": "/t.jsonl", "now": T1, "pid": 777})
    live = c.execute("SELECT id FROM sessions WHERE pid=777 AND stopped_at IS NULL").fetchall()
    assert len(live) == 1
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (live[0]["id"],)).fetchone()[0] == "fresh"
    assert c.execute("SELECT stopped_at FROM sessions WHERE id=?", (old,)).fetchone()["stopped_at"] == T1


def test_pid_claim_with_null_pid_is_noop(fresh):
    c = fresh()
    k = mk_session(c, session_id="keep", pid=321)
    c.execute(W["W2c_pid_claim"], {"pid": "", "session_id": "whoever", "now": T1})   # empty -> NULL
    assert c.execute("SELECT stopped_at FROM sessions WHERE id=?", (k,)).fetchone()["stopped_at"] is None
