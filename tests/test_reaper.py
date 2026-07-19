"""R — the liveness reaper (daemon.py): ps-driven death, boot sweep, and the
pure proc-table / _dead helpers."""
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

import herd.daemon as daemon
from herd.daemon import reap_once, boot_sweep, _parse_proc_table, _dead, read_proc_table

from helpers import T0, T1, T2, W, SOCK, mk_session, stopped_at

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


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


# ── an untrustworthy ps must NOT read as "the machine is idle" ───────────────
# _dead() treats absence as death, so an empty table reaps the entire herd in one
# tick. read_proc_table returns None (not {}) on every failure mode, and run()
# skips the reap. A real `ps -eo` always lists at least this process.
class _FakePs:
    def __init__(self, rc=0, out="", exc=None):
        self.rc, self.out, self.exc = rc, out, exc

    def __call__(self, *a, **k):
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(a[0], self.rc, self.out, "")


def test_read_proc_table_none_on_nonzero_ps(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", _FakePs(rc=1, out=""))
    assert read_proc_table() is None


def test_read_proc_table_none_on_empty_table(monkeypatch):
    """rc==0 but nothing parseable — a broken/blocked ps, not an empty machine."""
    monkeypatch.setattr(daemon.subprocess, "run", _FakePs(rc=0, out="\nbogus\n"))
    assert read_proc_table() is None


def test_read_proc_table_none_when_ps_is_missing(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", _FakePs(exc=FileNotFoundError("ps")))
    assert read_proc_table() is None


def test_read_proc_table_parses_a_good_table(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", _FakePs(rc=0, out="100 S claude\n"))
    assert read_proc_table() == {100: ("S", "claude")}


def test_broken_ps_reaps_nothing(fresh, monkeypatch):
    """The whole point: one failed probe must not stop every live session."""
    c = fresh()
    k = _live(c, "alive", 5003)
    monkeypatch.setattr(daemon.subprocess, "run", _FakePs(rc=1, out=""))
    procs = read_proc_table()
    assert procs is None
    if procs is not None:                     # mirrors run()'s guard
        reap_once(c, procs, T2)
    assert stopped_at(c, k) is None


# ── W3f: stranded spawn reservations ────────────────────────────────────────
# reap_once cannot touch these (pid IS NOT NULL by design), but R_job_live counts
# them live, so without the sweep a job name stays burned until the next reboot.
def _reservation(c, job, when=T0):
    from herd.spawn import W
    pk = c.execute(W["W1_spawn_session"], {"cwd": "/tmp", "now": when}).lastrowid
    c.execute(W["W1_spawn_herd"], {"pk": pk, "job": job, "now": when, "socket": "unix:/x"})
    return pk


def test_sweep_drops_a_reservation_that_never_became_a_session(fresh):
    c = fresh()
    pk = _reservation(c, "ghost")
    assert daemon.sweep_stranded(c, T2, max_age=60) == 1
    assert c.execute("SELECT COUNT(*) n FROM sessions WHERE id=?", (pk,)).fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM herd_sessions").fetchone()["n"] == 0   # CASCADE


def test_sweep_spares_a_reservation_still_mid_launch(fresh):
    """A reservation is legitimately pid-NULL for the span of the kitty round trip —
    sweeping it would kill a spawn that is about to succeed."""
    c = fresh()
    _reservation(c, "launching", when=T1)                    # 5 min before T2
    assert daemon.sweep_stranded(c, T2, max_age=600) == 0     # grace not yet spent
    assert stopped_at(c, 1) is None


def test_sweep_never_touches_an_adopted_session(fresh):
    """Only spawn reservations are pid-NULL AND session_id-NULL. A row a hook has
    adopted must survive regardless of age."""
    c = fresh()
    live = mk_session(c, session_id="adopted", pid=4242, started_at=T0)
    nopid = mk_session(c, session_id="hook-row-no-pid", started_at=T0)
    assert daemon.sweep_stranded(c, T2, max_age=60) == 0
    assert stopped_at(c, live) is None and stopped_at(c, nopid) is None


# ── the tick must survive its own failures ──────────────────────────────────
def test_a_failing_tick_is_logged_and_the_daemon_keeps_going(fresh, tmp_path, capsys, monkeypatch):
    """Every statement is its own autocommit txn against a WAL five hook scripts and
    a per-session statusline are also writing, so `database is locked` is routine.
    It used to exit the process: a restart loop into systemd's start limit, or — on
    macOS/headless where the service install is a documented no-op — silent-death
    reaping simply stopping, with nothing to notice.

    Crashing was also the de-facto recovery for a BROKEN HANDLE (systemd restarted
    us with a fresh one), so surviving means reopening the connection ourselves."""
    fresh(name="loop.db").close()
    db = str(tmp_path / "loop.db")
    ticks = {"n": 0}

    def fails_once(conn, procs, now):
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return 0

    monkeypatch.setattr(daemon, "reap_once", fails_once)
    monkeypatch.setattr(daemon, "read_proc_table", lambda: {})

    daemon.run(interval=0, db_path=db, once=True, attend=False)      # tick 1 raises
    err = capsys.readouterr().err
    assert "tick failed" in err and "database is locked" in err      # and says so

    daemon.run(interval=0, db_path=db, once=True, attend=False)      # tick 2 works
    assert ticks["n"] == 2, "the daemon stopped ticking after one failure"


def test_a_tick_failure_still_reaps_once_the_fault_clears(fresh, tmp_path, monkeypatch):
    """The point of surviving: work that failed this tick lands on the next one."""
    c = fresh(name="recover.db")
    dead = _live(c, "gone", 5000)
    c.close()
    db = str(tmp_path / "recover.db")
    monkeypatch.setattr(daemon, "read_proc_table", lambda: {})       # 5000 is absent

    real = daemon.sweep_stranded
    monkeypatch.setattr(daemon, "sweep_stranded",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    daemon.run(interval=0, db_path=db, once=True, attend=False)
    monkeypatch.setattr(daemon, "sweep_stranded", real)
    daemon.run(interval=0, db_path=db, once=True, attend=False)

    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    assert c.execute("SELECT stopped_at FROM sessions WHERE id=?", (dead,)).fetchone()[0]


# ── exactly one daemon ──────────────────────────────────────────────────────
def test_a_second_daemon_cannot_take_the_lock(tmp_path, monkeypatch):
    """Two daemons are easy to end up with — the systemd unit plus the manual
    `python3 -m herd.daemon` the README gives macOS/headless users — and they
    disagree quietly: both tick attention against different `now` values, so a
    session arms and disarms on alternating ticks and the mark flickers."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    monkeypatch.setattr(daemon, "_LOCK_FH", None)
    assert daemon.acquire_single_instance() is True
    assert daemon.holder_pid() == os.getpid()
    assert daemon.acquire_single_instance() is False      # no second holder


def test_main_refuses_to_start_a_second_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    monkeypatch.setattr(daemon, "_LOCK_FH", None)
    daemon.acquire_single_instance()
    assert daemon.main([]) == 1                            # nonzero, not a silent no-op
    assert "already running" in capsys.readouterr().err


def test_the_lock_dies_with_its_holder(tmp_path):
    """flock, not a pidfile: the kernel drops it however the process dies, so a -9
    or a crash can never leave a stale lock that blocks every future start."""
    lock = tmp_path / "herd-daemon.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; sys.path.insert(0, %r);"
         "import herd.daemon as d;"
         "assert d.acquire_single_instance(%r); print('held', flush=True); time.sleep(60)"
         % (str(SRC), str(lock))],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert daemon.acquire_single_instance(str(lock)) is False   # taken
    finally:
        holder.kill()
        holder.wait()
    assert daemon.acquire_single_instance(str(lock)) is True        # released by -9


# ── the reaper must not kill sessions it never observed as dead ───────────────
def test_boot_sweep_spares_a_resumed_session(fresh):
    """W2b_insert's ON CONFLICT deliberately preserves started_at, so a RESUMED
    session carries a pre-boot started_at with a live post-boot process. Sweeping on
    started_at alone reaped it — and since boot_time is fixed, re-reaped it on every
    later daemon start, undoing any manual recovery."""
    c = fresh()
    pre_boot, boot, after = "2026-07-14T08:00:00.000Z", "2026-07-15T09:00:00.000Z", "2026-07-15T09:30:00.000Z"
    c.execute(W["W2b_insert"], {"session_id": "abc", "cwd": "/x", "model": "m",
                                "transcript": "/t", "pid": 4242, "now": pre_boot})
    c.execute(W["W2b_insert"], {"session_id": "abc", "cwd": "/x", "model": "m",
                                "transcript": "/t", "pid": 9999, "now": after})   # resume
    r = c.execute("SELECT started_at,last_event_at FROM sessions").fetchone()
    assert r["started_at"] == pre_boot and r["last_event_at"] == after   # the trap
    boot_sweep(c, "2026-07-15T10:00:00.000Z", boot)
    assert stopped_at(c, 1) is None, "reaped a live resumed session"
    assert len(c.execute(W["R1_list"]).fetchall()) == 1


def test_boot_sweep_still_reaps_a_genuine_pre_boot_corpse(fresh):
    """The guard must not defang the sweep: a row that has done nothing since boot
    is exactly what it exists to clear."""
    c = fresh()
    mk_session(c, session_id="old", pid=4242, started_at="2026-07-14T08:00:00.000Z",
               last_event_at="2026-07-14T08:05:00.000Z")
    boot_sweep(c, "2026-07-15T10:00:00.000Z", "2026-07-15T09:00:00.000Z")
    assert stopped_at(c, 1) is not None


def test_boot_sweep_reaps_a_row_that_never_had_an_event(fresh):
    c = fresh()
    mk_session(c, session_id="never", pid=4242, started_at="2026-07-14T08:00:00.000Z",
               last_event_at=None)
    boot_sweep(c, "2026-07-15T10:00:00.000Z", "2026-07-15T09:00:00.000Z")
    assert stopped_at(c, 1) is not None


def test_reap_does_not_fire_when_the_pid_changed_since_the_select(fresh):
    """reap_once reads (id, pid), forks `ps` (up to 5s), then writes. A resume in
    that window installs a new pid and clears stopped_at; keyed on id alone the
    daemon reaped a live process it had never observed."""
    c = fresh()
    c.execute(W["W2b_insert"], {"session_id": "abc", "cwd": "/x", "model": "m",
                                "transcript": "/t", "pid": 4242, "now": T0})
    pk = c.execute("SELECT id FROM sessions").fetchone()[0]
    # the daemon judged pid 4242 dead; meanwhile the row moved to 7777
    c.execute(W["W2b_insert"], {"session_id": "abc", "cwd": "/x", "model": "m",
                                "transcript": "/t", "pid": 7777, "now": T1})
    n = c.execute(W["W3d_reap"], {"pk": pk, "now": T2, "pid": 4242}).rowcount
    assert n == 0
    assert stopped_at(c, pk) is None
    # and the ordinary case still reaps
    assert c.execute(W["W3d_reap"], {"pk": pk, "now": T2, "pid": 7777}).rowcount == 1


@pytest.mark.parametrize("raw,expect", [("-60", 120), ("-1", 120), ("0", 0), ("45", 45),
                                        ("fast", 120), ("", 120)])
def test_int_env_rejects_negatives(monkeypatch, raw, expect):
    """HERD_STRANDED_SECS=-60 put sweep_stranded's cutoff in the FUTURE, so it
    deleted every spawn reservation the instant it was created — each `herd spawn`
    lost its row while kitty was still starting. int() accepts -60; nothing else
    about it is meaningful."""
    if raw == "":
        monkeypatch.delenv("HERD_STRANDED_SECS", raising=False)
    else:
        monkeypatch.setenv("HERD_STRANDED_SECS", raw)
    assert daemon._int_env("HERD_STRANDED_SECS", 120) == expect


def test_a_negative_stranded_secs_does_not_sweep_an_inflight_reservation(fresh, monkeypatch):
    """The end of that chain: the reservation must survive its own launch."""
    monkeypatch.setenv("HERD_STRANDED_SECS", "-60")
    c = fresh()
    now = daemon._now_iso()
    pk = c.execute(W["W1_spawn_session"], {"cwd": "/x", "now": now}).lastrowid
    c.execute(W["W1_spawn_herd"], {"pk": pk, "job": "inflight", "now": now, "socket": SOCK})
    assert daemon.sweep_stranded(c, now, daemon._int_env("HERD_STRANDED_SECS", 120)) == 0
