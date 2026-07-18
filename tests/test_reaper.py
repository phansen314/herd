"""R — the liveness reaper (daemon.py): ps-driven death, boot sweep, and the
pure proc-table / _dead helpers."""
from herd.daemon import reap_once, boot_sweep, _parse_proc_table, _dead

from helpers import T0, T1, T2, mk_session, stopped_at


def _live(c, sid, pid, started=T0, le=T0):
    return mk_session(c, session_id=sid, pid=pid, status="working", status_source="hook",
                      last_event_at=le, last_event_type="tool", started_at=started, updated_at=started)


def test_reaper_reaps_dead_keeps_live(fresh):
    c = fresh()
    a = _live(c, "absent", 5000)
    z = _live(c, "zombie", 5001)
    r = _live(c, "recycled", 5002)
    ok = _live(c, "alive", 5003)
    procs = {5001: ("Z", "claude"), 5002: ("S", "bash"), 5003: ("S", "claude")}  # 5000 absent
    assert reap_once(c, procs, T2) == 3
    assert stopped_at(c, a) and stopped_at(c, z) and stopped_at(c, r) and stopped_at(c, ok) is None


def test_reaper_skips_null_pid(fresh):
    c = fresh()
    k = mk_session(c, session_id="nopid")
    assert reap_once(c, {}, T2) == 0 and stopped_at(c, k) is None


def test_reap_provenance_and_no_clock_move(fresh):
    c = fresh()
    d = _live(c, "dead", 5010)
    reap_once(c, {}, T2)
    row = c.execute("SELECT status,status_source,stopped_at,last_event_at FROM sessions WHERE id=?", (d,)).fetchone()
    assert (row["status"], row["status_source"], row["stopped_at"], row["last_event_at"]) == \
        ("stopped", "pid", T2, T0)


def test_reaper_is_idempotent(fresh):
    c = fresh()
    _live(c, "dead", 5010)
    reap_once(c, {}, T2)
    assert reap_once(c, {}, T2) == 0


def test_boot_sweep_reaps_preboot_spares_postboot(fresh):
    c = fresh()
    old = _live(c, "preboot", 6000, started=T0)
    new = _live(c, "postboot", 6001, started=T2)
    boot_sweep(c, T2, T1)   # boot at T1
    assert stopped_at(c, old) and stopped_at(c, new) is None


def test_boot_sweep_none_is_noop(fresh):
    c = fresh()
    x = _live(c, "x", 6002, started=T0)
    boot_sweep(c, T2, None)
    assert stopped_at(c, x) is None


def test_parse_proc_table():
    pp = _parse_proc_table("  100 Ss /usr/bin/claude\n200 Z claude\nbogus line\n300 R\n400 Sl+ node\n")
    assert pp == {100: ("S", "claude"), 200: ("Z", "claude"), 400: ("S", "node")}


def test_dead_verdicts():
    assert _dead(1, {}) and _dead(1, {1: ("Z", "claude")}) and _dead(1, {1: ("S", "bash")})
    assert not _dead(1, {1: ("S", "claude")})
