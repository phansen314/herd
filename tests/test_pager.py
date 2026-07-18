"""I — the pager lifecycle: arm (edge-preserving), page, ack, re-arm, and the
ack race guard."""
from helpers import W, T0, T0_10, T0_20, T1, T2, mk_session


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


# ── the attention_at <= focus_started_at race guard ──────────────────────────
# Both production call sites pass focus_started_at == now == the attention time, so
# only the positive case ever ran: flipping `<=` to `>=` left the suite green.
def _ack(c, pk, focus_started_at, now):
    c.execute(W["W6c_ack"], {"pk": pk, "now": now, "focus_started_at": focus_started_at})
    return c.execute("SELECT ack_at FROM herd_attention WHERE session_pk=?",
                     (pk,)).fetchone()[0]


def test_ack_clears_attention_raised_before_the_jump_started(fresh):
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0})       # raised first
    assert _ack(c, pk, focus_started_at=T0_10, now=T0_20) == T0_20


def test_ack_does_not_clear_attention_raised_mid_jump(fresh):
    """The whole point of the guard: a jump takes real time (kitty round-trip). An
    attention raised AFTER the jump began has not been seen by the user, so acking
    it would silently swallow a genuine notification."""
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0_20})    # raised DURING the jump
    assert _ack(c, pk, focus_started_at=T0_10, now=T1) is None


def test_ack_is_idempotent(fresh):
    """ack_at IS NULL keeps a second focus from overwriting the first ack time."""
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0})
    first = _ack(c, pk, focus_started_at=T0_10, now=T0_10)
    assert _ack(c, pk, focus_started_at=T1, now=T2) == first
