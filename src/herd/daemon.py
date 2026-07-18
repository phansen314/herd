"""herd daemon — the always-on liveness reaper. See DESIGN.md#liveness.

Hooks can't catch SILENT death (kill -9, crash, closed terminal) where no hook
fires and the row sits stopped_at IS NULL forever. This daemon reads the PROCESS
TABLE each tick and reaps sessions whose process is gone. Liveness comes from
`ps`, never from kitty (kitty absence is placement evidence, not death).

Two layers on one loop: the CORE reaper (tier 1, always) and the HERD attention
tick (tier 2, gated by HERD_ATTENTION — set 0 for core-only collection). IO
(read_proc_table / boot_time_iso) is split from logic so ticks are testable with
injected inputs.

    python -m herd.daemon          # reaper + attention (default)
    python -m herd.daemon --once   # one tick, then exit
    HERD_ATTENTION=0 python -m herd.daemon   # core-only: reaper, no attention
"""
import datetime
import os
import pathlib
import subprocess
import sys
import time

from herd.db import connect, load_statements

W = load_statements()

# Same identity anchor as claude_pid(); a node-based install overrides both via
# HERD_CLAUDE_NAME so hook and reaper stay consistent.
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# Attention: "needs attention" is DERIVED each tick from status + time-in-status
# (now - last_event_at), never stored. Thresholds in seconds, env-overridable.
# Statuses not listed are never page-worthy. See DESIGN.md#attention.
ATTENTION_SECS = {
    "waiting":        int(os.environ.get("HERD_WAIT_SECS", "30")),
    "needs_approval": int(os.environ.get("HERD_APPROVAL_SECS", "15")),
    "working":        int(os.environ.get("HERD_STUCK_SECS", "300")),
}


def _attention_enabled():
    """Gate the tier-2 attention tick. Default on; HERD_ATTENTION=0/false/no/off
    -> core-only (reaper runs, herd_attention untouched). See DESIGN.md#tiers."""
    return os.environ.get("HERD_ATTENTION", "1").strip().lower() not in ("0", "false", "no", "off")

# Matches common.sh: HERD_DB or ~/.herd/herd.db.
DEFAULT_DB = os.environ.get("HERD_DB", str(pathlib.Path.home() / ".herd" / "herd.db"))


def _now_iso():
    """ISO-UTC with millis, matching the hooks' NOW_ISO and sessions.started_at."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── IO (swappable in tests) ──────────────────────────────────────────────────
def _parse_proc_table(text):
    """`pid stat comm` lines -> {pid: (state_char, comm_basename)}. split(None, 2)
    keeps a comm containing spaces intact; junk/short lines are skipped."""
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
    """ONE ps fork per tick. Portable Linux+macOS (no /proc dependency). Returns
    None — NOT {} — when the table can't be trusted: a nonzero ps, an exec failure,
    or an empty parse. _dead() reads "absent from the table" as dead, so handing a
    caller {} on a broken ps reaps every live session at once. A real `ps -eo`
    always lists this process, so no rows means the probe failed, not that the
    machine is idle. Callers must skip the tick on None."""
    try:
        p = subprocess.run(["ps", "-eo", "pid=,stat=,comm="],
                           capture_output=True, text=True)
    except OSError:                              # ps missing from PATH, fork limit
        return None
    if p.returncode != 0:
        return None
    procs = _parse_proc_table(p.stdout)
    return procs or None


def boot_time_iso():
    """System boot as an ISO-UTC string comparable to started_at, or None off
    Linux / on failure (W3e is a backstop; W3d still reaps by liveness)."""
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
    """A stored pid is DEAD unless it is a live, non-zombie claude. Reap if:
    absent; zombie (state Z passes kill -0 but is gone); or comm != CLAUDE_NAME (a
    non-NULL pid was confirmed comm==claude at start, so a mismatch means the pid
    was recycled — the original claude died silently). See DESIGN.md#pid."""
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
    """One reap tick over live, pid-bearing sessions. pid-NULL rows are skipped
    (liveness unknowable; clean death arrives via SessionEnd). Returns count."""
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
    """Run ONCE at startup: reap live rows whose started_at precedes system boot
    (recycled pids could read dead sessions as alive). No-op when boot_time None."""
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


def _silent_for(status, since, now):
    """Seconds of silence past the status threshold, or None when the status isn't
    page-worthy / a stamp won't parse. Shared by the arm rule and the re-notify
    rule so both measure the same way against the same knobs."""
    secs = ATTENTION_SECS.get(status)
    if secs is None:
        return None
    a, b = _epoch(since), _epoch(now)
    if a is None or b is None:
        return None
    return (b - a) - secs


def needs_attention(status, last_event_at, now):
    """True when a session has been in a page-worthy status longer than its
    threshold. Unlisted statuses / unparseable stamps -> False."""
    over = _silent_for(status, last_event_at, now)
    return over is not None and over >= 0


def attention_tick(conn, now):
    """Keep herd_attention in sync with the derived silence rule: arm what newly
    needs attention (W6a, edge-preserving), disarm what no longer does (W6d), and
    let an acked row's timer run out so a session you looked at but never answered
    speaks up again (W6d, then a fresh W6a next tick).
    Returns (armed, disarmed). See DESIGN.md#attention."""
    armed = disarmed = 0
    rows = conn.execute(
        "SELECT s.id, s.status, s.last_event_at, a.ack_at, "
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
        elif na and r["is_armed"] and r["ack_at"]:
            # RE-NOTIFY. A jump acks the silence and the CLI stops rendering the mark,
            # but the session is still silent and still unanswered. ack_at restarts
            # the same per-status timer: once THAT much silence has passed since the
            # ack, drop the row. The next tick's W6a re-arms with a fresh
            # attention_at and ack_at NULL, and the mark comes back.
            #
            # Dropping the row is also why the ack can't simply be "disarm on jump":
            # W6d is a whole-row DELETE, so it takes ack_at with it. Deleting on the
            # ack itself would leave the next tick measuring from last_event_at,
            # which is still old — it would re-arm immediately and flap every tick.
            over = _silent_for(r["status"], r["ack_at"], now)
            if over is not None and over >= 0:
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
        procs = read_proc_table()
        if procs is not None:                         # tier 1 — always, unless ps
            reap_once(conn, procs, now)               # is untrustworthy this tick
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
