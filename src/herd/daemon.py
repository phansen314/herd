"""herd daemon — the always-on liveness reaper.

Hooks push what Claude reports; they mark a session stopped on SessionEnd. They
cannot catch SILENT death — kill -9, crash, closed terminal, kitty quit — where no
hook fires and the row sits `stopped_at IS NULL` forever: an immortal row that still
reads live, holds its job/window, occupies a pid in idx_sessions_pid_live, and would
page you about a session that's gone. You cannot hook the ABSENCE of an event, so a
poller is the only thing that can notice.

This daemon reads the PROCESS TABLE each tick and reaps sessions whose process is
gone. Liveness comes from `ps`, NEVER from kitty (see writes.sql W3d): absence from
a kitty `ls` is placement evidence, and reaping on it would mass-reap every session
on a socket blip / allow_remote_control off / an `ls` timeout. When a process is
really gone, `ps` says so within a tick.

NOT the TUI. It runs whether or not any UI is open, because a dead session must free
its resources and stop paging you even when you are not looking. This is what
"reconcile" shrinks to now that hooks own identity, placement, and pid: the
`kitten @ ls` half is gone; a `ps` loop remains.

It soft-marks `stopped_at` (NULL -> timestamp) and NEVER deletes rows — this is
live-set correctness, not table-size management. Pruning is a separate concern.

IO (read_proc_table / boot_time_iso) is split from logic (reap_once / boot_sweep /
_dead) so the tick is testable with an injected process table — the same discipline
as claude_pid()/_walk_claude() in hooks/common.sh. The canonical W3d/W3e statements
are loaded through herd.db.load_statements(), never inlined, so the daemon and the
suite exercise the SAME SQL.

    python -m herd.daemon          # run the reap loop
    python -m herd.daemon --once   # one boot-sweep + reap tick, then exit
"""
import datetime
import os
import pathlib
import subprocess
import sys
import time

from herd.db import connect, load_statements

W = load_statements()

# Same identity anchor as claude_pid(): the process is claude iff its comm basename
# matches this. A node-based install overrides both via HERD_CLAUDE_NAME so the hook
# (which finds the pid) and the reaper (which keeps it alive) stay consistent.
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# Matches common.sh: HERD_DB or ~/.herd/herd.db.
DEFAULT_DB = os.environ.get("HERD_DB", str(pathlib.Path.home() / ".herd" / "herd.db"))


def _now_iso():
    """ISO-UTC with millis, matching the hooks' NOW_ISO and sessions.started_at."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── IO (swappable in tests) ──────────────────────────────────────────────────
def _parse_proc_table(text):
    """`pid ppid?/stat/comm` lines -> {pid: (state_char, comm_basename)}.

    We call `ps -eo pid=,stat=,comm=`; comm is the last field. split(None, 2) keeps
    a comm that contains spaces intact. Junk/short lines are skipped, not fatal.
    """
    procs = {}
    for line in text.splitlines():
        f = line.split(None, 2)
        if len(f) < 3:
            continue
        try:
            pid = int(f[0])
        except ValueError:
            continue
        state = f[1][:1]                       # first char: R/S/D/Z/...
        comm = f[2].strip().rsplit("/", 1)[-1]  # basename (macOS comm is a full path)
        procs[pid] = (state, comm)
    return procs


def read_proc_table():
    """ONE ps fork per tick. Portable Linux+macOS (no /proc dependency)."""
    out = subprocess.run(["ps", "-eo", "pid=,stat=,comm="],
                         capture_output=True, text=True).stdout
    return _parse_proc_table(out)


def boot_time_iso():
    """System boot as an ISO-UTC string comparable to started_at, or None.

    Linux /proc/stat `btime` (epoch seconds). None off Linux / on failure — W3e is a
    reboot backstop, so skipping it is safe (W3d still reaps by liveness every tick).
    """
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime"):
                    bt = int(line.split()[1])
                    return (datetime.datetime.fromtimestamp(bt, datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    except (OSError, ValueError, IndexError):
        pass
    return None


# ── liveness + the tick ──────────────────────────────────────────────────────
def _dead(pid, procs):
    """A stored pid is DEAD unless it is a live, non-zombie claude.

    Reap if: absent from the table; a zombie (state Z still passes kill -0 but the
    process is gone); or its comm no longer matches CLAUDE_NAME. The comm check
    CANNOT false-reap a live session — a non-NULL pid means the hook's claude_pid()
    confirmed comm==claude at start, so a mismatch now means the pid was recycled to
    an unrelated process (the original claude died silently). That is exactly a reap.
    """
    p = procs.get(pid)
    if p is None:
        return True
    state, comm = p
    if state == "Z":
        return True
    if comm != CLAUDE_NAME:
        return True
    return False


def reap_once(conn, procs, now):
    """One reap tick. Reaps every live, pid-bearing session whose pid is dead.

    pid-NULL rows are skipped: liveness is unknowable for them, and their clean death
    arrives via SessionEnd. Returns the number reaped.
    """
    reaped = 0
    rows = conn.execute(
        "SELECT id, pid FROM sessions WHERE stopped_at IS NULL AND pid IS NOT NULL"
    ).fetchall()
    for r in rows:
        if _dead(r["pid"], procs):
            conn.execute(W["W3d_reap"], {"pk": r["id"], "now": now})
            reaped += 1
    return reaped


def boot_sweep(conn, now, boot_time):
    """Run ONCE at startup. Reaps live rows whose started_at precedes system boot —
    after a reboot their pids may have been recycled, so a plain liveness check could
    read a dead session as alive. No-op when boot_time is None."""
    if boot_time:
        conn.execute(W["W3e_boot_sweep"], {"now": now, "boot_time": boot_time})


# ── driver ───────────────────────────────────────────────────────────────────
def run(interval=2.0, db_path=None, once=False):
    conn = connect(db_path or DEFAULT_DB)   # one RW connection: busy_timeout + WAL
    boot_sweep(conn, _now_iso(), boot_time_iso())
    while True:
        reap_once(conn, read_proc_table(), _now_iso())
        if once:
            return
        time.sleep(interval)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    run(once="--once" in argv)


if __name__ == "__main__":
    main()
