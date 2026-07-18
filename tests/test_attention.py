"""S — the attention tick (daemon.py): the derived silence rule, arm/disarm edge
handling, and the HERD_ATTENTION core/herd layer gate."""
import datetime as dt

import pytest

from herd import cli
from herd.daemon import (needs_attention, attention_tick, _attention_enabled,
                         run as daemon_run)

from helpers import T0, T1, T2, T0_10, T0_20, T0_240, W, mk_session, mk_attention


@pytest.mark.parametrize("status,le,now,expect", [
    ("waiting", T0, T1, True),                 # trips after 30s grace
    ("waiting", T0, T0, False),
    ("needs_approval", T0, T0_20, True),        # ~15s grace
    ("needs_approval", T0, T0_10, False),
    ("working", T0, T1, True),                  # 'stuck' after ~5min
    ("working", T0, T0_240, False),
    ("stopped", T0, T2, False),                 # never page-worthy
    ("unknown", T0, T2, False),
    ("working", None, T2, False),               # NULL last_event never trips
])
def test_needs_attention(status, le, now, expect):
    assert needs_attention(status, le, now) is expect


def _att(c, pk):
    r = c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()
    return r["attention_at"] if r else None


def test_tick_arms_past_threshold_not_fresh(fresh):
    c = fresh()
    w = mk_session(c, session_id="w", status="waiting", last_event_at=T0, last_event_type="stop")
    f = mk_session(c, session_id="f", status="waiting", last_event_at=T1, last_event_type="stop",
                   started_at=T1, updated_at=T1)
    armed, dis = attention_tick(c, T1)
    assert armed == 1 and dis == 0 and _att(c, w) == T1 and _att(c, f) is None


def test_tick_preserves_edge(fresh):
    c = fresh()
    w = mk_session(c, session_id="w", status="waiting", last_event_at=T0, last_event_type="stop")
    attention_tick(c, T1)
    c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (T2, w))
    attention_tick(c, T2)   # still waiting on the same last_event_at
    assert _att(c, w) == T1


def test_tick_disarms_when_silence_clears(fresh):
    c = fresh()
    d = mk_session(c, session_id="d", status="working", last_event_at=T1, last_event_type="tool",
                   started_at=T1, updated_at=T1)
    mk_attention(c, d, attention_at=T0)   # was armed
    armed, dis = attention_tick(c, T1)
    assert dis == 1 and armed == 0 and _att(c, d) is None


def test_tick_ignores_stopped(fresh):
    c = fresh()
    s = mk_session(c, session_id="s", status="waiting", last_event_at=T0, last_event_type="stop",
                   stopped_at=T1)
    armed, _ = attention_tick(c, T2)
    assert armed == 0 and _att(c, s) is None


# ── ack: a jump silences the row; ack_at restarts the same timer ─────────────
# The '!' is rendered by cli, the timer is run by the daemon, and the bug this
# guards against was the two disagreeing — so these drive both halves.
T1_10 = "2026-07-15T10:05:10.000Z"   # ack + 10s: inside the 30s waiting threshold
T1_40 = "2026-07-15T10:05:40.000Z"   # ack + 40s: past it


def _waiting(c):
    """A session that has been silent long enough to arm at T1."""
    return mk_session(c, session_id="w", status="waiting",
                      last_event_at=T0, last_event_type="stop")


def _ack(c, pk, now):
    c.execute(W["W6c_ack"], {"pk": pk, "now": now, "focus_started_at": now})


def _row(c, pk):
    return c.execute("SELECT attention_at, ack_at FROM herd_attention WHERE session_pk=?",
                     (pk,)).fetchone()


def _bang(c):
    """The rendered '!' column, straight through R1_list — the real read path."""
    return [cli._line(r)[0] for r in cli._live(c)]


def test_ack_silences_the_render_but_keeps_the_row(fresh):
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    assert _bang(c) == ["!"]
    _ack(c, w, T1)
    assert _bang(c) == [" "]                     # quiet
    assert _row(c, w)["ack_at"] == T1            # but still armed, still recorded


def test_acked_row_does_not_flap(fresh):
    """The trap: W6d is a whole-row DELETE, so disarming an acked row would drop
    ack_at and the next tick would re-arm from the old last_event_at, every tick."""
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    for _ in range(3):
        armed, dis = attention_tick(c, T1_10)
        assert (armed, dis) == (0, 0)
    r = _row(c, w)
    assert (r["attention_at"], r["ack_at"]) == (T1, T1)
    assert _bang(c) == [" "]


def test_ack_timer_renotifies(fresh):
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    armed, dis = attention_tick(c, T1_40)        # ack's own silence ran out
    assert (armed, dis) == (0, 1) and _row(c, w) is None
    armed, dis = attention_tick(c, T1_40)        # next tick re-arms fresh
    assert (armed, dis) == (1, 0)
    r = _row(c, w)
    assert (r["attention_at"], r["ack_at"]) == (T1_40, None)
    assert _bang(c) == ["!"]                     # and it speaks up again


def test_activity_clears_an_acked_row(fresh):
    """Real work still wins over the ack timer, via the existing disarm branch."""
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    c.execute("UPDATE sessions SET status='working', last_event_at=? WHERE id=?", (T1_40, w))
    armed, dis = attention_tick(c, T1_40)
    assert (armed, dis) == (0, 1) and _row(c, w) is None


@pytest.mark.parametrize("val,expect", [(None, True), ("0", False), ("off", False), ("1", True)])
def test_attention_enabled_gate(monkeypatch, val, expect):
    if val is None:
        monkeypatch.delenv("HERD_ATTENTION", raising=False)
    else:
        monkeypatch.setenv("HERD_ATTENTION", val)
    assert _attention_enabled() is expect


def _db_path(c):
    return next(r[2] for r in c.execute("PRAGMA database_list") if r[1] == "main")


def test_run_gates_attention_end_to_end(fresh):
    """core-only writes zero herd_attention; herd mode arms a waiting session.
    started_at=now survives the boot sweep; last_event 5min ago trips the rule."""
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    c = fresh()
    mk_session(c, session_id="wf", status="waiting",
               last_event_at=iso(now - dt.timedelta(seconds=300)), last_event_type="stop",
               started_at=iso(now), updated_at=iso(now - dt.timedelta(seconds=300)))
    dbp = _db_path(c)
    daemon_run(db_path=dbp, once=True, attend=False)
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 0
    daemon_run(db_path=dbp, once=True, attend=True)
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 1
