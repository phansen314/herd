"""L — R1_list: the TUI main read uses the live index, hits tier-2 PKs directly,
and orders attention-first."""
from helpers import W, T0, T1, T2, mk_session, mk_herd, mk_attention


def _seed(c):
    for i, (cwd, st, ev) in enumerate([("/app", "waiting", "stop"),
                                       ("/api", "working", "tool"),
                                       ("/web", "working", "tool")]):
        p = mk_session(c, session_id=f"s{i}", cwd=cwd, status=st,
                       last_event_at=T0, last_event_type=ev, updated_at=T2)
        mk_herd(c, p, job_name=f"job{i}", created_at=T0,
                kitty_socket="unix:/tmp/k1", window_id=i + 1, verified_at=T2)
        if i == 0:
            mk_attention(c, p, attention_at=T1)


def test_query_plan_uses_live_index_and_pk_joins(fresh):
    c = fresh()
    _seed(c)
    plan = [r[-1] for r in c.execute("EXPLAIN QUERY PLAN " + W["R1_list"])]
    assert any("idx_sessions_live" in p for p in plan)
    assert sum("INTEGER PRIMARY KEY" in p for p in plan) == 2   # both tier-2 joins hit PK


def test_attention_first_ordering(fresh):
    c = fresh()
    _seed(c)
    rows = c.execute(W["R1_list"]).fetchall()
    assert rows[0]["cwd"] == "/app" and rows[0]["attention_at"] == T1
