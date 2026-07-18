"""D / 70 — W2b_placement (the living herd_sessions writer): a hook re-fire
preserves the immutable job identity and updates only the placement columns."""
import pytest

from helpers import W, T0, T2, SOCK, mk_session, mk_herd


@pytest.fixture
def refired(fresh):
    """A spawned session, then a hook re-fire from a NEW window."""
    c = fresh()
    pk = mk_session(c, session_id="u1")
    mk_herd(c, pk, job_name="api-refactor", created_at=T0,
            kitty_socket="unix:/tmp/kitty-1", window_id=7, herd_var="api-refactor")
    c.execute(W["W2b_placement"], {"session_id": "u1", "socket": "unix:/tmp/kitty-9",
                                   "win": 42, "now": T2})
    return c.execute("SELECT job_name,created_at,kitty_socket,window_id,source,herd_var,"
                     "verified_at FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()


@pytest.mark.parametrize("col,want", [
    ("job_name", "api-refactor"),      # immutable
    ("created_at", T0),                # immutable
    ("herd_var", "api-refactor"),      # immutable (hook can't know the spawn var)
    ("source", "spawn"),               # provenance must not decay to 'hook'
    ("window_id", 42),                 # mutable
    ("kitty_socket", "unix:/tmp/kitty-9"),  # mutable
    ("verified_at", T2),               # mutable
])
def test_refire_mutability_contract(refired, col, want):
    assert refired[col] == want


def test_w2b_placement_records_hook_window(fresh):
    """70 — a user-started claude gets a placement row (source=hook, no job),
    landing on the row W2b_insert wrote in the same transaction."""
    c = fresh()
    c.execute(W["W2b_insert"], {"session_id": "u1", "cwd": "/code/herd", "model": "opus",
                                "transcript": "/t.jsonl", "now": T0, "pid": None})
    c.execute(W["W2b_placement"], {"session_id": "u1", "socket": SOCK, "win": 8, "now": T0})
    r = c.execute("SELECT h.source,h.kitty_socket,h.window_id,h.job_name "
                  "FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                  "WHERE s.session_id='u1'").fetchone()
    assert (r["source"], r["kitty_socket"], r["window_id"], r["job_name"]) == ("hook", SOCK, 8, None)
