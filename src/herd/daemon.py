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

from herd import config as _config
from herd.db import connect, load_statements

W = load_statements()

# MUST stay before the constants below, which read the env at import. Kept as
# module state rather than logged: _log is not defined yet here, and cli.py imports
# this module, so logging would fire on every `herd ls`. run() reports it once.
CONFIG_APPLIED, CONFIG_SHADOWED, CONFIG_PROBLEMS = _config.apply()


def _now_iso():
    """ISO-UTC with millis, matching the hooks' NOW_ISO and sessions.started_at."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Cap for a stderr that is a FILE — launchd's StandardErrorPath is a plain file
# nothing truncates, and KeepAlive restarts on any exit. 0 disables.
# See DECISIONS.md#launchd-log.
#
# BOUND FIRST as a literal, overridden below once _int_env exists: _int_env reports
# malformed values through _log, and _log reads this name during the constant block.
DAEMON_LOG_MAX = 1048576


def _stderr_is_a_regular_file():
    try:
        import stat
        return stat.S_ISREG(os.fstat(sys.stderr.fileno()).st_mode)
    except (OSError, ValueError, AttributeError):
        return False


def _truncate_stderr_if_huge():
    """Truncate rather than rotate, in place. launchd holds the file open in append
    mode, so RENAMING it would leave the daemon writing to an unlinked inode. With
    O_APPEND a truncate sends the next write to offset 0."""
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
    """Daemon diagnostics -> stderr (the journal under systemd). The hooks'
    HERD_ERRLOG is theirs, not ours.

    Bounded when stderr is a regular file (launchd) — see DAEMON_LOG_MAX; a no-op
    under systemd and in a terminal."""
    if _truncate_stderr_if_huge():
        print(f"{_now_iso()} herd.daemon: log exceeded {DAEMON_LOG_MAX} bytes — truncated",
              file=sys.stderr, flush=True)
    print(f"{_now_iso()} herd.daemon: {msg}", file=sys.stderr, flush=True)


def _int_env(name, default):
    """Env int with a fallback. A malformed threshold must not take down every herd
    command: these are read at IMPORT time and cli.py imports this module."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        _log(f"{name}={raw!r} is not an integer — using {default}")
        return default
    # A NEGATIVE grace period is a cutoff in the FUTURE, not a shorter one:
    # HERD_STRANDED_SECS=-60 sweeps every spawn reservation the instant it is
    # created. Zero is left alone — "no grace" is a coherent choice.
    if val < 0:
        _log(f"{name}={raw!r} is negative — using {default}")
        return default
    return val


# Same identity anchor as claude_pid(); a node-based install overrides both via
# HERD_CLAUDE_NAME so hook and reaper stay consistent.
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# Linux caps comm at TASK_COMM_LEN-1 = 15 chars, so a CLAUDE_NAME of 16+ can never
# equal what ps reports — and _dead reads a comm mismatch as a recycled pid, i.e.
# death. macOS reports the full basename, hence _is_claude accepting both forms.
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

# The literal above is the default this overrides; both must stay in step.
DAEMON_LOG_MAX = _int_env("HERD_DAEMON_LOG_MAX", DAEMON_LOG_MAX)

# Ceiling for the retry backoff. A permanent fault (HERD_DB pointing at a non-herd
# file, a full disk) must keep retrying — a locked DB IS transient — but not at the
# 2s cadence, which cost ~86k journal lines a day.
BACKOFF_MAX_SECS = _int_env("HERD_BACKOFF_MAX_SECS", 60)

# One tick's `ps` must not outlive the tick: a hung ps freezes the reaper while the
# process stays alive, so systemd's Restart never fires.
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
    keeps a comm containing spaces intact; junk lines are skipped."""
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
    None — NOT {} — when the table can't be trusted: nonzero ps, exec failure, or an
    empty parse (a real `ps -eo` always lists this process, so no rows means the
    probe failed). _dead() reads absence as death, so {} would reap everything.
    Callers MUST skip the tick on None."""
    try:
        p = subprocess.run(["ps", "-eo", "pid=,stat=,comm="],
                           capture_output=True, text=True, timeout=PS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
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
def _is_claude(comm):
    """Does this comm name our process? Exact, or the Linux-truncated form — see
    _COMM_MAX. Truncating the STORED name, not the observed one, keeps a short
    impostor out: `cla` never passes for `claude`."""
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
    """Same anchor as lock_path(), cli._runtime_dir() and the hooks' HERD_RUNTIME.
    A resolver that disagrees takes a different lock and runs a second daemon."""
    return _config.runtime_dir()


def _valid_sid(sid):
    """The hooks' valid_sid, in python: a session_id becomes a FILENAME, so refuse
    anything that could leave the runtime dir."""
    return bool(sid) and all(c.isalnum() or c == "-" for c in sid)


def sweep_runtime_files(session_id):
    """Delete the per-session files the hooks keep — the throttle stamp and the
    statusline cache.

    session_end.sh does this too, but SessionEnd does not fire on kill -9, a crash,
    or a closed terminal — the deaths this daemon exists to reap. The reaper is the
    only thing that learns about those, so the cleanup belongs here as well.
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


# Grace before a per-session runtime file with no live row is treated as garbage.
# The statusline can render (and cache) before SessionStart commits the row, so a
# young file with no row is NORMAL. Generous on purpose: waiting costs one stale
# file, being wrong deletes a live session's cache mid-startup.
ORPHAN_GRACE_SECS = _int_env("HERD_ORPHAN_GRACE_SECS", 300)


def sweep_orphan_files(conn, now=None, listdir=None, unlink=None, age_of=None):
    """Delete per-session runtime files whose session is gone or stopped.

    DIRECTORY-driven, unlike sweep_runtime_files, and that is the whole point: a
    row-driven sweep needs a session_id from the DB, so it can never reach a file
    whose row is gone. boot_sweep (W3e) leaks such a pair per session by
    construction — it stops pre-boot sessions and touches none of their files.

    Files younger than ORPHAN_GRACE_SECS are left alone; see that constant.
    """
    d = _runtime_dir()
    listdir = listdir or (lambda: os.listdir(d))
    unlink = unlink or (lambda pth: os.unlink(pth))
    age_of = age_of or (lambda pth: time.time() - os.stat(pth).st_mtime)
    try:
        names = listdir()
    except OSError:
        return 0                      # dir gone or unreadable: nothing to do
    live = {r[0] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE stopped_at IS NULL "
        "AND session_id IS NOT NULL")}
    gone = 0
    for name in names:
        for prefix in ("herd-stline-", "herd-tool-"):
            if not name.startswith(prefix):
                continue
            sid = name[len(prefix):]
            # This name came off the filesystem and is about to become a path to
            # unlink: `herd-stline-../../x` is not a session.
            if not _valid_sid(sid) or sid in live:
                break
            pth = os.path.join(d, name)
            try:
                if age_of(pth) < ORPHAN_GRACE_SECS:
                    break             # too young to judge — see the docstring
                unlink(pth)
                gone += 1
            except OSError:
                pass                  # raced with session_end.sh, or not ours
            break
    return gone


def reap_once(conn, procs, now, recheck=read_proc_table):
    """One reap tick over live, pid-bearing sessions. pid-NULL rows are skipped
    (liveness unknowable; clean death arrives via SessionEnd). Returns count.

    `procs` is read BEFORE this SELECT, so absence from it is ambiguous: the pid
    died, OR its SessionStart landed between the two reads and it was never in the
    snapshot. Reaping a newborn is PERMANENT — W4_event carries `AND stopped_at IS
    NULL`, so only a fresh SessionStart clears it — and re-asserting the pid in
    W3d_reap does not help, since the pid judged IS the newborn's own.

    So a candidate is confirmed against a SECOND snapshot taken after the SELECT. A
    process born before the SELECT must appear in it; one that died before the first
    snapshot is absent from both. Only absence from both is death. The re-check is
    lazy — most ticks have no candidate and fork nothing."""
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
        return 0            # untrustworthy ps: skip the tick. Treating a failed
                            # re-check as "confirmed absent" reaps everything.
    for r in candidates:
        # _dead again, not bare membership: a pid that reappears as a zombie or as a
        # recycled non-claude is still dead, and those signals only exist on presence.
        if _dead(r["pid"], confirm):
            # Pass the pid we JUDGED, not the row's current one — W3d_reap re-asserts
            # it, so a resume that landed since the SELECT is a 0-row no-op.
            n = conn.execute(
                W["W3d_reap"], {"pk": r["id"], "now": now, "pid": r["pid"]}).rowcount
            reaped += n
            # Only when the reap landed: 0 rows means the session resumed since the
            # SELECT and its files are live again.
            if n:
                sweep_runtime_files(r["session_id"])
    return reaped


def _shift_iso(now, secs):
    """`now` minus `secs`, as an ISO-UTC stamp comparable to the stored ones, or
    None when `now` will not parse."""
    at = _epoch(now)
    if at is None:
        return None
    return (datetime.datetime.fromtimestamp(at - secs, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")


def sweep_stranded(conn, now, max_age=None):
    """Drop spawn reservations that never became a session. Returns count.

    reap_once cannot: its pid predicate skips pid-NULL rows, so without this they
    hold their job_name until the next boot sweep. Independent of `ps` — the row
    names no process."""
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
    """ISO-UTC stamp (with or without millis) -> epoch seconds, or None."""
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
    page-worthy / a stamp won't parse. Shared by the arm and re-notify rules."""
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
            # between the SELECT and this write means the session is no longer
            # silent, and the statement declines.
            cutoff = _shift_iso(now, ATTENTION_SECS[r["status"]])
            if cutoff is None:
                continue
            armed += conn.execute(
                W["W6a_arm"], {"pk": r["id"], "now": now, "cutoff": cutoff}).rowcount
        elif not na and r["is_armed"]:
            disarmed += conn.execute(W["W6d_rearm"], {"pk": r["id"]}).rowcount
        elif na and r["is_armed"] and r["ack_at"]:
            # RE-NOTIFY. An ack hides the mark but the session is still silent and
            # unanswered, so ack_at restarts the same per-status timer; drop the row
            # once it expires and the next tick's W6a re-arms fresh.
            #
            # Deleting on the ack ITSELF would flap: W6d is a whole-row DELETE, so it
            # takes ack_at with it, and the next tick would measure from the still-old
            # last_event_at and re-arm immediately.
            over = _silent_for(r["status"], r["ack_at"], now)
            if over is not None and over >= 0:
                disarmed += conn.execute(W["W6d_rearm"], {"pk": r["id"]}).rowcount
    return armed, disarmed


# ── driver: single instance ──────────────────────────────────────────────────────────
# Exactly one daemon. Two (systemd unit + a manual `python3 -m herd.daemon`) tick
# attention against different `now` values, so a session arms and disarms on
# alternating ticks and the mark flickers.
#
# flock, not a pidfile: the kernel releases it however the process dies, so a -9
# cannot leave a stale lock. The handle MUST stay alive for the process lifetime —
# closing it, including by garbage collection, drops the lock.
_LOCK_FH = None


def lock_path():
    return os.path.join(_runtime_dir(), "herd-daemon.lock")


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
            # The open succeeded and the flock did not. Safe to close: flock is per
            # open-file-description, so dropping ours cannot disturb the holder.
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


def _backoff(fails, interval):
    """How long to wait before the next tick, given consecutive failures.

    fails <= 1 is the normal cadence on purpose: one bad tick is usually a held
    write lock, which clears in milliseconds. Beyond that it doubles, capped at
    BACKOFF_MAX_SECS.

    NEVER BELOW `interval`. _int_env permits 0 by design — "no grace" is coherent
    for a grace period — but a 0 CAP is not: it made this return 0 for every failing
    tick, so `time.sleep(0)` spun the loop as fast as the machine allowed, hammering
    the WAL write lock and starving the hooks' busy_timeout, on exactly the permanent
    faults (schema-less DB, full disk) the cap exists to slow down. Any cap below
    `interval` is the same bug smaller: it makes FAILING ticks poll faster than
    healthy ones."""
    if fails <= 1:
        return interval
    return max(interval, min(interval * (2 ** min(fails - 1, 10)), BACKOFF_MAX_SECS))


def _fault_hint(exc, db):
    """Turn the two faults that are really misconfiguration into the sentence that
    fixes them, or None."""
    msg = str(exc).lower()
    if "no such table" in msg:
        return (f"{db} is not a herd database (no schema) — check HERD_DB in "
                f"~/.herd/config, or run: python3 -m herd.install")
    if "unable to open database file" in msg:
        return (f"cannot open {db} — check HERD_DB in ~/.herd/config, or run: "
                f"python3 -m herd.install")
    return None


def run(interval=2.0, db_path=None, once=False, attend=None):
    """CORE reaper every tick; HERD attention tick only when enabled (attend, or
    HERD_ATTENTION when attend is None).

    A TICK MAY FAIL WITHOUT ENDING THE DAEMON. Every statement here is its own
    autocommit transaction taking the WAL write lock, against five hook scripts and
    a per-session statusline doing the same, so `database is locked` is normal, not
    fatal. Since we no longer exit, nothing else restarts us with a fresh handle —
    so a failed tick must reopen the connection itself (see _drop), or a dead handle
    would fail every tick forever.
    """
    if attend is None:
        attend = _attention_enabled()
    # Say what the config file did, ONCE. A shadowed key is the interesting line:
    # the file says one thing and this process is doing another.
    for key, val in sorted(CONFIG_APPLIED.items()):
        _log(f"config: {key}={val}")
    for key, (want, got) in sorted(CONFIG_SHADOWED.items()):
        _log(f"config: {key}={want} IGNORED — the environment sets {key}={got}")
    for msg in CONFIG_PROBLEMS:
        _log(f"config: {msg}")
    db = db_path or DEFAULT_DB
    conn = None
    swept = False                                     # boot sweep runs once, when we
    fails = 0                                         # first hold a usable connection
    last_err = None                                   # dedupe the log line
    while True:
        now = _now_iso()
        try:
            if conn is None:
                conn = connect(db)                    # RW: busy_timeout + WAL, no
                                                      # create — see db.connect
            if not swept:
                boot_sweep(conn, now, boot_time_iso())
                # Right after boot_sweep, and not on every tick: that is the one
                # moment the orphan set is largest. Later deaths are handled by
                # reap_once, which HAS the row.
                n = sweep_orphan_files(conn, now)
                if n:
                    _log(f"swept {n} orphaned runtime file(s)")
                swept = True
            procs = read_proc_table()
            if procs is not None:                     # tier 1 — always, unless ps
                reap_once(conn, procs, now)           # is untrustworthy this tick
            sweep_stranded(conn, now)                 # tier 1 — needs no proc table
            if attend:
                attention_tick(conn, now)             # tier 2 — herd's opinion
            # NOT gated on `attend`. W6e is garbage collection, not an opinion:
            # under HERD_ATTENTION=0 nothing else would ever clear these rows.
            sweep_dead_attention(conn)
            # Only on a tick that actually WORKED — an opened handle is not recovery
            # (a schema-less DB opens fine and fails every statement).
            if fails:
                _log(f"recovered after {fails} failed tick(s)")
            fails = 0
        except Exception as e:                        # noqa: BLE001 — a daemon outlives its errors
            fails += 1
            msg = f"{type(e).__name__}: {e}"
            # Log when the message CHANGES, and on a thinning schedule otherwise.
            if msg != last_err or fails in (1, 2, 3, 10, 30) or fails % 100 == 0:
                hint = _fault_hint(e, db)
                _log(f"tick failed ({msg}) — {fails} in a row, retrying"
                     + (f". {hint}" if hint else ""))
                last_err = msg
            conn = _drop(conn)                        # force a fresh handle next tick
        if once:
            return
        time.sleep(_backoff(fails, interval))


_FLAGS = {"--once", "--help", "-h"}

USAGE = """usage: python3 -m herd.daemon [--once]

  (no flags)    reaper + attention tick, every 2s, until killed
  --once        one tick, then exit
  --help, -h    this message

  HERD_ATTENTION=0   core-only: reaper runs, herd_attention untouched"""


def main(argv=None):
    """Unknown argv is REFUSED, not ignored — as in herd.install.main. A bare
    `"--once" in argv` test would start an endless daemon on `--onec` or `--help`.
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
