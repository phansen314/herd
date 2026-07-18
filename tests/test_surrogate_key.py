"""C — the surrogate id spine: tier-2 rows exist before Claude reports a UUID,
and adoption is a plain UPDATE that preserves them."""
from helpers import W, T0, T1, mk_session, mk_herd, mk_attention

_ADOPT = {"session_id": "a3f9-uuid", "cwd": "/code/app", "model": "opus",
          "transcript": "/t.jsonl", "now": T1, "pid": 4242,
          "socket": "unix:/tmp/kitty-1", "win": 7}


def _spawned(c):
    pk = mk_session(c, cwd="/code/app", status="unknown", status_source="reconcile")
    mk_herd(c, pk, job_name="api-refactor", created_at=T0,
            kitty_socket="unix:/tmp/kitty-1", window_id=7, herd_var="api-refactor")
    mk_attention(c, pk, attention_at=T0)
    return pk


def test_tier2_rows_exist_before_uuid(fresh):
    c = fresh()
    pk = _spawned(c)
    assert c.execute("SELECT session_id FROM sessions WHERE id=?", (pk,)).fetchone()[0] is None


def test_w2_adopt_via_socket_window(fresh):
    c = fresh()
    _spawned(c)
    assert c.execute(W["W2_adopt"], _ADOPT).rowcount == 1


def test_tier2_survives_adoption(fresh):
    c = fresh()
    pk = _spawned(c)
    c.execute(W["W2_adopt"], _ADOPT)
    r = c.execute("SELECT s.session_id,h.job_name,h.window_id,a.attention_at "
                  "FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk=s.id "
                  "LEFT JOIN herd_attention a ON a.session_pk=s.id WHERE s.id=?", (pk,)).fetchone()
    assert tuple(r) == ("a3f9-uuid", "api-refactor", 7, T0)


def test_w2_adopt_is_idempotent(fresh):
    c = fresh()
    _spawned(c)
    c.execute(W["W2_adopt"], _ADOPT)
    assert c.execute(W["W2_adopt"], _ADOPT).rowcount == 0   # re-fire is a no-op


def test_many_unadopted_rows_coexist(fresh):
    c = fresh()
    mk_session(c, cwd="/x")
    mk_session(c, cwd="/y")
    assert c.execute("SELECT COUNT(*) FROM sessions WHERE session_id IS NULL").fetchone()[0] == 2
