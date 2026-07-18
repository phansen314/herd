"""I — the pager lifecycle: arm (edge-preserving), page, ack, re-arm."""
from helpers import W, T0, T1, T2, mk_session


def test_pager_lifecycle(fresh):
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting", last_event_at=T0, last_event_type="stop")

    c.execute(W["W6a_arm"], {"pk": pk, "now": T1})
    c.execute(W["W6a_arm"], {"pk": pk, "now": T2})   # tick again — must not move the edge
    assert c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == T1

    c.execute(W["W6b_paged"], {"now": T2, "level": 2, "pk": pk})
    c.execute(W["W6c_ack"], {"now": T2, "pk": pk, "focus_started_at": T2})
    r = c.execute("SELECT attention_at,paged_at,paged_level,ack_at FROM herd_attention "
                  "WHERE session_pk=?", (pk,)).fetchone()
    assert tuple(r) == (T1, T2, 2, T2)

    c.execute(W["W6d_rearm"], {"pk": pk})
    assert c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == 0

    c.execute(W["W6a_arm"], {"pk": pk, "now": T2})   # rule can trip fresh after re-arm
    assert c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == T2
