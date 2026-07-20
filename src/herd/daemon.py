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
import fcntl
import os
import pathlib
import subprocess
import sys
import time

from herd.db import connect, load_statements

W = load_statements()


def _now_iso():
    """ISO-UTC with millis, matching the hooks' NOW_ISO and sessions.started_at."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Cap for a stderr that is a FILE. journald rotates and a terminal scrolls, but
# launchd's StandardErrorPath is a plain file nothing ever truncates — and
# KeepAlive restarts the daemon on ANY exit, including the exit(1) it takes when
# another instance holds the lock. Running the daemon by hand (which README still
# documents) alongside the LaunchAgent is therefore a permanent 5s restart loop
# writing one line each time: ~17k lines/day, forever, into the file README points
# you at. 0 disables. See DECISIONS.md#launchd-log.
#
# BOUND FIRST, as a literal, and overridden below once _int_env exists. "_log
# resolves it at call time" is true of every call except the ones that happen
# DURING the constant block: _int_env reports a malformed value through _log, and
# _log reads this name, so a bad HERD_WAIT_SECS raised NameError at import instead
# of the ValueError _int_env was written to swallow — and cli.py imports this
# module, so `HERD_WAIT_SECS=fast herd ls` tracebacked. Same for a bad
# HERD_DAEMON_LOG_MAX, which reports through _log while defining itself.
DAEMON_LOG_MAX = 1048576


def _stderr_is_a_regular_file():
    try:
        import stat
        return stat.S_ISREG(os.fstat(sys.stderr.fileno()).st_mode)
    except (OSError, ValueError, AttributeError):
        return False


def _truncate_stderr_if_huge():
    """Truncate rather than rotate, in place. launchd holds the file open in append
    mode, so RENAMING it would leave the daemon writing to an unlinked inode and the
    visible log permanently empty — the opposite of the intent. With O_APPEND a
    truncate simply sends the next write to offset 0."""
    if not DAEMON_LOG_MAX or not _stderr_is_a_regular_file():
        return False
    try:
        if os.fstat(sys.stderr.fileno()).st_size <= DAEMON_LOG_MAX:
            return False
        os.ftruncate(sys.stderr.fileno(), 0)
        return True
    except OSError:
        return False


def _log(msg):
    """Daemon diagnostics -> stderr. systemd captures that into the journal, which
    is where README sends you (`journalctl --user -u herd`); a foreground daemon
    shows it inline. The hooks' HERD_ERRLOG is theirs, not ours.

    Bounded when stderr is a file (launchd) — see DAEMON_LOG_MAX. A no-op under
    systemd and in a terminal, where stderr is not a regular file."""
    if _truncate_stderr_if_huge():
        print(f"{_now_iso()} herd.daemon: log exceeded {DAEMON_LOG_MAX} bytes — truncated",
              file=sys.stderr, flush=True)
    print(f"{_now_iso()} herd.daemon: {msg}", file=sys.stderr, flush=True)


def _int_env(name, default):
    """A malformed threshold must not take down every herd command. These are read
    at IMPORT time and cli.py imports this module, so `HERD_WAIT_SECS=fast herd ls`
    used to be a ValueError traceback with no hint which variable was at fault."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        _log(f"{name}={raw!r} is not an integer — using {default}")
        return default
    # A NEGATIVE grace period is not a shorter grace period, it is a cutoff in the
    # FUTURE. HERD_STRANDED_SECS=-60 made sweep_stranded delete every spawn
    # reservation the instant it was created — verified: a brand-new in-flight
    # reservation swept on the first tick, so every `herd spawn` loses its row while
    # kitty is still starting. The negative attention thresholds are milder (arm
    # instantly, always) but equally not what anyone meant. This function exists so a
    # malformed threshold cannot break things; -60 is malformed in every sense except
    # int() accepting it. Zero is left alone — "no grace" is a coherent choice.
    if val < 0:
        _log(f"{name}={raw!r} is negative — using {default}")
        return default
    return val


# Same identity anchor as claude_pid(); a node-based install overrides both via
# HERD_CLAUDE_NAME so hook and reaper stay consistent.
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# Linux caps a process's comm at TASK_COMM_LEN-1 = 15 chars (`ps -o comm=` reads
# /proc/pid/stat), so a CLAUDE_NAME of 16+ can NEVER equal what ps reports. _dead
# reads a comm mismatch as a recycled pid, so that override reaped every live
# session on the first tick. macOS reports the full basename, hence both forms.
_COMM_MAX = 15

# Attention: "needs attention" is DERIVED each tick from status + time-in-status
# (now - last_event_at), never stored. Thresholds in seconds, env-overridable.
# Statuses not listed are never page-worthy. See DESIGN.md#attention.
ATTENTION_SECS = {
    "waiting":        _int_env("HERD_WAIT_SECS", 30),
    "needs_approval": _int_env("HERD_APPROVAL_SECS", 15),
    "working":        _int_env("HERD_STUCK_SECS", 300),
}


# Grace before a pid-NULL spawn reservation is swept as stranded. Must comfortably
# exceed a kitty launch round trip + claude's startup to its first hook.
STRANDED_SECS = _int_env("HERD_STRANDED_SECS", 120)

# Bound for a stderr that is a FILE — see _truncate_stderr_if_huge. 0 disables.
# The literal above is the default this overrides; both must stay in step.
DAEMON_LOG_MAX = _int_env("HERD_DAEMON_LOG_MAX", DAEMON_LOG_MAX)

# One tick's `ps` must not outlive the tick. Generous — a loaded box can be slow —
# but finite, because a hung ps freezes the reaper while the process stays alive,
# so systemd's Restart never fires.
PS_TIMEOUT = 5


def _attention_enabled():
    """Gate the tier-2 attention tick. Default on; HERD_ATTENTION=0/false/no/off
    -> core-only (reaper runs, herd_attention untouched). See DESIGN.md#tiers."""
    return os.environ.get("HERD_ATTENTION", "1").strip().lower() not in ("0", "false", "no", "off")

# Matches common.sh: HERD_DB or ~/.herd/herd.db.
DEFAULT_DB = os.environ.get("HERD_DB", str(pathlib.Path.home() / ".herd" / "herd.db"))


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
                           capture_output=True, text=True, timeout=PS_TIMEOUT)
    except subprocess.TimeoutExpired:            # a wedged ps would freeze the loop
        return None                              # forever — and systemd's restart
    except OSError:                              # never fires on a LIVE process
        return None                              # ps missing from PATH, fork limit
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
def _is_claude(comm):
    """Does this comm name our process? Exact, or the Linux-truncated form of a
    CLAUDE_NAME too long to survive /proc — see _COMM_MAX. Truncating the STORED
    name (not the observed one) keeps this from matching a short impostor: `cla`
    never passes for `claude`."""
    return comm == CLAUDE_NAME or comm == CLAUDE_NAME[:_COMM_MAX]


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
    if not _is_claude(comm):
        return True
    return False


def _runtime_dir():
    """Same anchor as lock_path(), cli._runtime_dir() and the hooks' HERD_RUNTIME."""
    return os.environ.get("HERD_RUNTIME",
                          os.environ.get("XDG_RUNTIME_DIR", "/tmp"))


def _valid_sid(sid):
    """The hooks' valid_sid, in python: a session_id becomes a FILENAME, so refuse
    anything that could leave the runtime dir."""
    return bool(sid) and all(c.isalnum() or c == "-" for c in sid)


def sweep_runtime_files(session_id):
    """Delete the per-session files the hooks keep — the throttle stamp and the
    statusline cache.

    session_end.sh already does this ("or they leak one pair per session forever
    — bounded on a tmpfs $XDG_RUNTIME_DIR, unbounded under the /tmp fallback"),
    but SessionEnd does not fire on kill -9, a crash, or a closed terminal. Those
    are the deaths THIS DAEMON exists to reap, so every one of them left a pair
    behind: 34 herd-stline-* files against 1 live session, measured. The reaper is
    the only thing that learns about them, so the cleanup belongs here.
    """
    if not _valid_sid(session_id):
        return 0
    gone = 0
    for name in (f"herd-tool-{session_id}", f"herd-stline-{session_id}"):
        try:
            os.unlink(os.path.join(_runtime_dir(), name))
            gone += 1
        except OSError:
            pass                      # never created, or already cleaned
    return gone


def reap_once(conn, procs, now, recheck=read_proc_table):
    """One reap tick over live, pid-bearing sessions. pid-NULL rows are skipped
    (liveness unknowable; clean death arrives via SessionEnd). Returns count.

    `procs` is read BEFORE this SELECT, so absence from it is ambiguous: the pid
    died, OR its SessionStart landed between the two reads and it was never in the
    snapshot to begin with. _dead() reads absence as death, so the newborn case was
    a reap — and a permanent one, because W4_event carries `AND stopped_at IS NULL`
    and only a fresh SessionStart (W2b_insert) clears it. A session running fine in
    its terminal went invisible to R1_list for the rest of its life. Re-asserting
    the pid in W3d_reap does not help: the pid we judged IS the newborn's own.

    So a candidate is confirmed against a SECOND snapshot taken after the SELECT. A
    process born before the SELECT is alive now and must appear in it; one that died
    before the first snapshot is absent from both. Only absence from both is death.
    The re-check is lazy — most ticks have no candidate and fork nothing."""
    reaped = 0
    rows = conn.execute(
        "SELECT id, pid, session_id FROM sessions "
        "WHERE stopped_at IS NULL AND pid IS NOT NULL"
    ).fetchall()
    candidates = [r for r in rows if _dead(r["pid"], procs)]
    if not candidates:
        return 0
    confirm = recheck()
    if confirm is None:
        return 0            # untrustworthy ps: skip the tick, exactly as run() does
                            # on the first snapshot. Treating a failed re-check as
                            # "confirmed absent" would reap every session at once.
    for r in candidates:
        # _dead again, not bare membership: a pid that reappears as a zombie or as a
        # recycled non-claude is still dead, and those signals only exist on presence.
        if _dead(r["pid"], confirm):
            # Pass the pid we JUDGED, not the row's current one — W3d_reap re-asserts
            # it, so a resume that landed since the SELECT is a 0-row no-op instead of
            # a live session reaped on evidence about a pid it no longer holds.
            n = conn.execute(
                W["W3d_reap"], {"pk": r["id"], "now": now, "pid": r["pid"]}).rowcount
            reaped += n
            # Only when the reap actually landed: a 0-row result means the session
            # resumed since the SELECT, and its files are live again.
            if n:
                sweep_runtime_files(r["session_id"])
    return reaped


def _shift_iso(now, secs):
    """`now` minus `secs`, as an ISO-UTC stamp comparable to the stored ones, or
    None when `now` will not parse. Shared by sweep_stranded and the arm rule so a
    cutoff is computed one way."""
    at = _epoch(now)
    if at is None:
        return None
    return (datetime.datetime.fromtimestamp(at - secs, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")


def sweep_stranded(conn, now, max_age=None):
    """Drop spawn reservations that never became a session. reap_once cannot: its
    pid predicate skips them by design, so without this they hold their job_name
    live until the next boot sweep. Independent of `ps` — nothing to check, the row
    names no process. Returns count."""
    age = STRANDED_SECS if max_age is None else max_age
    cutoff = _shift_iso(now, age)
    if cutoff is None:
        return 0
    return conn.execute(W["W3f_sweep_stranded"], {"cutoff": cutoff}).rowcount


def sweep_dead_attention(conn):
    """Reclaim herd_attention rows whose session has stopped. Tier 2 — runs with
    the attention tick, which cannot do it itself: that tick only visits live rows,
    so the orphan it would need to see is filtered out before it looks."""
    return conn.execute(W["W6e_sweep_dead"]).rowcount


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
            # Re-assert the silence at write time — see W6a_arm. A hook firing
            # between this tick's SELECT and this write means the session is no
            # longer silent, and the statement declines rather than marking it.
            cutoff = _shift_iso(now, ATTENTION_SECS[r["status"]])
            if cutoff is None:
                continue
            armed += conn.execute(
                W["W6a_arm"], {"pk": r["id"], "now": now, "cutoff": cutoff}).rowcount
        elif not na and r["is_armed"]:
            # rowcount, like `armed` — this counted intent, not effect, so a W6d
            # that matched nothing still reported a disarm.
            disarmed += conn.execute(W["W6d_rearm"], {"pk": r["id"]}).rowcount
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
                disarmed += conn.execute(W["W6d_rearm"], {"pk": r["id"]}).rowcount
    return armed, disarmed


# ── driver ───────────────────────────────────────────────────────────────────
# ── single instance ──────────────────────────────────────────────────────────
# Exactly one daemon, no way around it. Two are easy to end up with — the systemd
# unit plus the manual `python3 -m herd.daemon` the README tells macOS/headless
# users to run — and the damage is quiet: both tick attention against different
# `now` values, so a session can arm and disarm on alternating ticks and the mark
# flickers. Neither process is wrong; they just disagree.
#
# flock, not a pidfile: the kernel releases it on death however the process dies,
# so a -9 or a crash cannot leave a stale lock that blocks every future start.
# The handle is deliberately kept alive for the process lifetime — closing it,
# including by letting it be garbage collected, drops the lock.
_LOCK_FH = None


def lock_path():
    return os.path.join(os.environ.get("HERD_RUNTIME",
                        os.environ.get("XDG_RUNTIME_DIR", "/tmp")), "herd-daemon.lock")


def acquire_single_instance(path=None):
    """Take the daemon lock. True if we now hold it, False if another daemon does."""
    global _LOCK_FH
    path = path or lock_path()
    fh = None
    try:
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fh is not None:
            # The open SUCCEEDED and the flock did not, so this handle was left to
            # the garbage collector. CPython's refcounting closes it at return, so
            # this was never an observable leak — it is explicit because the
            # guarantee is an implementation detail, not a promise. Safe either way:
            # flock is per open-file-description, so dropping ours cannot disturb
            # the daemon that holds the lock.
            fh.close()
        return False
    fh.seek(0); fh.truncate(); fh.write(f"{os.getpid()}\n"); fh.flush()
    _LOCK_FH = fh                        # MUST outlive this function — see above
    return True


def holder_pid(path=None):
    """The pid recorded by whoever holds the lock, for a useful refusal message."""
    try:
        with open(path or lock_path()) as fh:
            return int(fh.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def _drop(conn):
    """Close a connection we no longer trust, and return None so the next tick
    reopens. Never raises — this runs on the failure path."""
    try:
        if conn is not None:
            conn.close()
    except Exception:                                 # noqa: BLE001
        pass
    return None


def run(interval=2.0, db_path=None, once=False, attend=None):
    """CORE reaper every tick; HERD attention tick only when enabled (attend, or
    HERD_ATTENTION when attend is None).

    A TICK MAY FAIL WITHOUT ENDING THE DAEMON. Every statement here is its own
    autocommit transaction taking the WAL write lock, against five hook scripts and
    a per-session statusline doing the same, so `database is locked` is a normal
    event, not a fatal one. It used to exit the process: under systemd that meant a
    restart loop into the default start limit and a `failed` unit; on macOS or
    headless, where install_service is a documented no-op, it meant silent-death
    reaping simply stopped with nothing to notice.

    Crashing was also the de-facto recovery for a BROKEN HANDLE (systemd restarted
    us with a fresh one), so surviving a failure means we have to reopen the
    connection ourselves — otherwise a dead handle would fail every tick forever and
    reap nothing, which is the same silence with none of the restart.
    """
    if attend is None:
        attend = _attention_enabled()
    db = db_path or DEFAULT_DB
    conn = None
    swept = False                                     # boot sweep runs once, when we
    fails = 0                                         # first hold a usable connection
    while True:
        now = _now_iso()
        try:
            if conn is None:
                conn = connect(db)                    # RW: busy_timeout + WAL
                if fails:
                    _log("reconnected")
            if not swept:
                boot_sweep(conn, now, boot_time_iso())
                swept = True
            procs = read_proc_table()
            if procs is not None:                     # tier 1 — always, unless ps
                reap_once(conn, procs, now)           # is untrustworthy this tick
            sweep_stranded(conn, now)                 # tier 1 — needs no proc table
            if attend:
                attention_tick(conn, now)             # tier 2 — herd's opinion
            # NOT gated on `attend`. W6e is garbage collection, not an opinion: it
            # deletes herd_attention rows whose session is already stopped. Gating
            # it meant HERD_ATTENTION=0 leaked those rows forever — verified — which
            # is the unbounded growth W6e exists to prevent, in the one mode where
            # nothing else would ever clear them.
            sweep_dead_attention(conn)
            fails = 0
        except Exception as e:                        # noqa: BLE001 — a daemon outlives its errors
            fails += 1
            _log(f"tick failed ({type(e).__name__}: {e}) — {fails} in a row, retrying")
            conn = _drop(conn)                        # force a fresh handle next tick
        if once:
            return
        time.sleep(interval)


_FLAGS = {"--once", "--help", "-h"}

USAGE = """usage: python3 -m herd.daemon [--once]

  (no flags)    reaper + attention tick, every 2s, until killed
  --once        one tick, then exit
  --help, -h    this message

  HERD_ATTENTION=0   core-only: reaper runs, herd_attention untouched"""


def main(argv=None):
    """Unknown argv is REFUSED, not ignored — as in herd.install.main.

    `run(once="--once" in argv)` was a membership test with no validation, so
    `--onec` started a daemon that never exits where a single tick was asked for,
    and `--help` started one too. --once is the flag you reach for while debugging,
    which is exactly when a silently-ignored typo costs the most.
    """
    argv = argv if argv is not None else sys.argv[1:]
    unknown = [a for a in argv if a not in _FLAGS]
    if unknown:
        _log(f"unknown option {', '.join(repr(a) for a in unknown)} — not starting")
        print(USAGE, file=sys.stderr)
        return 2
    if "--help" in argv or "-h" in argv:
        print(USAGE, file=sys.stderr)
        return 0
    if not acquire_single_instance():
        other = holder_pid()
        _log(f"another herd daemon is already running{f' (pid {other})' if other else ''}"
             " — refusing to start a second one")
        return 1
    run(once="--once" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
