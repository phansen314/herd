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

The loop carries TWO layers (see _attention_enabled): the CORE reaper (tier 1,
always) and the HERD attention tick (tier 2, gated by HERD_ATTENTION). Run herd for
pure core data collection with HERD_ATTENTION=0 and build your own tooling on the
sessions table; the tier-1/tier-2 seam the schema enforces is honored here too.

IO (read_proc_table / boot_time_iso) is split from logic (reap_once / boot_sweep /
_dead / needs_attention / attention_tick) so ticks are testable with injected
inputs — the same discipline as claude_pid()/_walk_claude() in hooks/common.sh. The
canonical W3d/W3e/W6 statements load through herd.db.load_statements(), never
inlined, so the daemon and the suite exercise the SAME SQL.

    python -m herd.daemon          # reaper + attention (default)
    python -m herd.daemon --once   # one tick, then exit
    HERD_ATTENTION=0 python -m herd.daemon   # core-only: reaper, no herd_attention
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

# ── attention: the silence rule ──────────────────────────────────────────────
# "Needs attention" is DERIVED here every tick from a session's status + how long
# it has been in it (now - last_event_at), never stored as a flag. What persists in
# herd_attention is the EDGE (attention_at: when the rule first tripped). A session
# is page-worthy when Claude is blocked ON YOU or wedged:
#   waiting        — turn ended, Claude wants input; grace before it's "waiting on you"
#   needs_approval — a permission prompt is up; shorter grace
#   working        — no new event in a long time -> likely stuck
# Thresholds are seconds, env-overridable for tuning. Statuses not listed (stopped,
# unknown) are never page-worthy. Actually NOTIFYING you (notify-send / a TUI badge)
# is a separate actuator, deliberately deferred — this chunk maintains the signal.
ATTENTION_SECS = {
    "waiting":        int(os.environ.get("HERD_WAIT_SECS", "30")),
    "needs_approval": int(os.environ.get("HERD_APPROVAL_SECS", "15")),
    "working":        int(os.environ.get("HERD_STUCK_SECS", "300")),
}


def _attention_enabled():
    """The daemon has TWO layers on one loop:
      CORE (tier 1)  — the reaper: writes sessions.stopped_at from ps liveness. A
                       fact true whether or not herd exists. ALWAYS runs.
      HERD (tier 2)  — the attention tick: writes herd_attention, herd's OPINION
                       about which sessions need YOU. Gated by HERD_ATTENTION.
    Default on. Set HERD_ATTENTION=0/false/no/off for CORE-ONLY collection: the
    sessions table stays maintained, herd_attention is never touched, and you can
    build your own tooling on top. This keeps the tier-1/tier-2 seam the schema
    enforces visible at the process boundary too."""
    return os.environ.get("HERD_ATTENTION", "1").strip().lower() not in ("0", "false", "no", "off")

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


# ── attention tick ───────────────────────────────────────────────────────────
def _epoch(iso):
    """Parse an ISO-UTC stamp (with or without millis) to epoch seconds, or None."""
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return (datetime.datetime.strptime(iso, fmt)
                    .replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def needs_attention(status, last_event_at, now):
    """True when a session has been in a page-worthy status longer than its
    threshold. Unlisted statuses (stopped/unknown) and unparseable stamps -> False."""
    secs = ATTENTION_SECS.get(status)
    if secs is None:
        return False
    a, b = _epoch(last_event_at), _epoch(now)
    if a is None or b is None:
        return False
    return (b - a) >= secs


def attention_tick(conn, now):
    """Keep herd_attention in sync with the derived silence rule. Arms a session
    that newly needs attention (W6a, edge-preserving) and disarms one that no longer
    does (W6d). The daemon owns the derived edge; ack/paging (deferred) layer on top.
    Returns (armed, disarmed) counts."""
    armed = disarmed = 0
    rows = conn.execute(
        "SELECT s.id, s.status, s.last_event_at, "
        "       (a.session_pk IS NOT NULL) AS is_armed "
        "FROM sessions s LEFT JOIN herd_attention a ON a.session_pk = s.id "
        "WHERE s.stopped_at IS NULL"
    ).fetchall()
    for r in rows:
        na = needs_attention(r["status"], r["last_event_at"], now)
        if na and not r["is_armed"]:
            conn.execute(W["W6a_arm"], {"pk": r["id"], "now": now})
            armed += 1
        elif not na and r["is_armed"]:
            conn.execute(W["W6d_rearm"], {"pk": r["id"]})
            disarmed += 1
    return armed, disarmed


# ── driver ───────────────────────────────────────────────────────────────────
def run(interval=2.0, db_path=None, once=False, attend=None):
    """CORE reaper every tick; HERD attention tick only when enabled (attend, or
    HERD_ATTENTION when attend is None)."""
    if attend is None:
        attend = _attention_enabled()
    conn = connect(db_path or DEFAULT_DB)   # one RW connection: busy_timeout + WAL
    boot_sweep(conn, _now_iso(), boot_time_iso())
    while True:
        now = _now_iso()
        reap_once(conn, read_proc_table(), now)       # tier 1 — always
        if attend:
            attention_tick(conn, now)                 # tier 2 — herd's opinion
        if once:
            return
        time.sleep(interval)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    run(once="--once" in argv)


if __name__ == "__main__":
    main()
