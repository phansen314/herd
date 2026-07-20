"""herd config — the settings file the daemon and the hooks BOTH read.

    ~/.herd/config          # KEY=value, # comments, blank lines ignored

This exists because the environment is not a shared channel between herd's parts.
The hooks are descendants of your shell and see everything you export; the daemon
is started by `systemctl --user`, which inherits NOTHING from a login shell. Its
unit names PYTHONPATH and nothing else, so its real environment (verified via
/proc) holds essentially nothing herd-shaped. So every threshold README documents — HERD_WAIT_SECS and friends,
read only by daemon.py — was silently ignored, and two of them were worse than
ignored:

  HERD_CLAUDE_NAME  the hooks saw it and stored a pid for a process named e.g.
                    `myclaude`; the daemon did not, compared comm against its own
                    default `claude`, read the mismatch as a recycled pid, and
                    reaped EVERY LIVE SESSION on the first tick. Verified.
  HERD_RUNTIME      lock_path() resolves under it, so a hand-started daemon took a
                    DIFFERENT lock file and ran alongside the systemd one — the
                    duplicate the flock exists to prevent.

Both are divergence bugs, not lookup bugs: the fix is one source of truth that
both readers agree on, which is this file. common.sh parses it with the same rules
(see load_config there) and test_source_invariants pins the two key lists together.

PRECEDENCE: a real environment variable WINS. The file supplies what the
environment does not set, so `HERD_WAIT_SECS=5 python3 -m herd.daemon --once` still
works for a one-off. The unit deliberately sets no HERD_* at all, so nothing
competes with this file in normal operation.
A shadowed key is REPORTED, never silently dropped — a config line that does
nothing is the bug this module was written to end.
"""
import os
import pathlib

# Every key herd reads anywhere — daemon, CLI, and hooks. A file key outside this
# set is a typo (HERD_WAIT_SEC, HERD_STUCK_SECONDS) and is reported rather than
# ignored: a misspelled tuning knob that stays silent is indistinguishable from
# one that is being obeyed, which is the whole failure this file addresses.
KNOWN = (
    # daemon
    "HERD_ATTENTION", "HERD_WAIT_SECS", "HERD_APPROVAL_SECS", "HERD_STUCK_SECS",
    "HERD_STRANDED_SECS", "HERD_DAEMON_LOG_MAX",
    # shared by the daemon and the hooks — the divergence pair
    "HERD_CLAUDE_NAME", "HERD_RUNTIME", "HERD_DB",
    # hooks
    "HERD_TOOL_THROTTLE", "HERD_ERRLOG", "HERD_ERRLOG_MAX",
    # cli
    "HERD_TEMPLATES",
)


def config_path():
    """The file, overridable for tests and for a non-default herd root."""
    p = os.environ.get("HERD_CONFIG")
    return pathlib.Path(p) if p else pathlib.Path.home() / ".herd" / "config"


def runtime_dir(env=None, mkdir=True):
    """The one directory for herd's per-session runtime files, the daemon lock, and
    the picker handoff. HERD_RUNTIME, else XDG_RUNTIME_DIR, else ~/.herd/run.

    NEVER /tmp, which is what the fallback used to be. Every name under it is
    predictable — herd-db-err.<pid>, herd-stline-<uuid>, herd-daemon.lock — and the
    hooks create them with plain redirects, which FOLLOW SYMLINKS. On a shared box
    another user could pre-create any of those as a link to a file of ours and have
    a hook truncate it. XDG_RUNTIME_DIR does not have that problem (/run/user/<uid>
    is 0700 and ours), and ~/.herd/run does not either; only /tmp did.

    ONE definition, because every reader has to agree: the hooks write the caches,
    the CLI reads them, and the daemon takes its single-instance lock here. Two
    answers means two locks and two daemons — which is what HERD_RUNTIME in a shell
    used to cause, and what ~/.herd/config now exists to prevent."""
    env = os.environ if env is None else env
    d = env.get("HERD_RUNTIME") or env.get("XDG_RUNTIME_DIR")
    if d:
        return d
    # env["HOME"], not expanduser("~"): expanduser reads the REAL environment, so a
    # caller passing an env dict (every test, and doctor asking what the DAEMON
    # resolves) got this process's home and the parameter silently did nothing.
    # common.sh uses $HOME here, and the two have to land on the same directory.
    home = env.get("HOME") or os.path.expanduser("~")
    d = os.path.join(home, ".herd", "run")
    # Only on the fallback path, and only when absent — this is called on the hook
    # hot path and an unconditional makedirs would be a syscall per statusline tick.
    if mkdir and not os.path.isdir(d):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)          # makedirs honours umask; be explicit
        except OSError:
            pass                        # unwritable HOME: callers degrade already
    return d


def parse(text):
    """(values, problems) from KEY=value lines. Never raises — this is read on the
    import path of every herd command, so a mangled file must degrade to "no
    settings" plus a complaint, not a traceback that takes out `herd ls`."""
    values, problems = {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {n}: no '=' in {line!r}")
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        # `export FOO=bar` is what muscle memory types into a file like this, and
        # silently binding a key named "export FOO" would be exactly the quiet
        # no-op this module exists to prevent. Accept it and move on.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key not in KNOWN:
            problems.append(f"line {n}: unknown key {key!r}")
            continue
        if key in values:
            problems.append(f"line {n}: {key} set twice — the later wins")
        # A LEADING ~ ONLY. Nothing expands it otherwise: this file is not read by a
        # shell, and common.sh assigns the value with eval "$k=\$v" — quoted, so bash
        # does not expand it either. The shipped template shows `~/.herd/herd.db`, so
        # uncommenting that line would have pointed the database at a directory
        # literally named "~". No $VAR expansion, deliberately: one rule that both
        # parsers can implement identically beats a shell-alike that drifts.
        if val.startswith("~"):
            val = os.path.expanduser(val)
        values[key] = val
    return values, problems


def load(path=None):
    """(values, problems) for the config file, or ({}, []) when there is none.
    A missing file is the normal case and says nothing."""
    p = pathlib.Path(path) if path else config_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"cannot read {p}: {e}"]
    return parse(text)


def apply(path=None, env=None):
    """Fill the environment from the file, WITHOUT overriding what is already set.
    Returns (applied, shadowed, problems) so a caller can report all three.

    Called once at import from daemon.py, which cli.py imports — so every python
    entry point gets the same settings, and the hooks get them from common.sh
    reading the same file by the same rules."""
    env = os.environ if env is None else env
    values, problems = load(path)
    applied, shadowed = {}, {}
    for key, val in values.items():
        if env.get(key) is not None:
            # Set in BOTH places. Not an error — a one-off override is a feature,
            # and the test suite exports these — but never silent: the file says one
            # thing and the process is doing another.
            if env.get(key) != val:
                shadowed[key] = (val, env[key])
            continue
        env[key] = val
        applied[key] = val
    return applied, shadowed, problems


DEFAULT_TEXT = """\
# herd config — read by the daemon AND the hooks. KEY=value, # starts a comment.
#
# This file exists because they do not share an environment: the hooks inherit
# your shell, the daemon is started by systemd and inherits nothing. Anything set
# here reaches both. A real environment variable still wins over this file, and a
# key that gets shadowed that way is reported by `herd doctor` rather than
# silently ignored.
#
# Uncomment and edit. Defaults are shown.

# ── attention thresholds (seconds) ──────────────────────────────────────────
#HERD_ATTENTION=1          # 0/off -> core-only: reaper runs, no attention
#HERD_WAIT_SECS=30         # grace before a `waiting` session needs you
#HERD_APPROVAL_SECS=15     # grace before a `needs_approval` prompt does
#HERD_STUCK_SECS=300       # silence before a `working` session reads as stuck
#HERD_STRANDED_SECS=120    # grace before an unstarted spawn reservation is dropped

# ── identity and placement ──────────────────────────────────────────────────
# HERD_CLAUDE_NAME MUST live here rather than in .bashrc. The hooks would see it
# there and the daemon would not, and that divergence makes the reaper read every
# live session as a recycled pid and stop all of them on its first tick.
#HERD_CLAUDE_NAME=claude   # process name the pid ancestry walk looks for
#HERD_RUNTIME=             # per-session runtime files + the daemon lock.
                           # Defaults to $XDG_RUNTIME_DIR, else ~/.herd/run.
                           # Setting this
                           # in a shell only splits the lock and runs two daemons.

# ── paths ───────────────────────────────────────────────────────────────────
#HERD_DB=~/.herd/herd.db   # authoritative: the systemd unit sets no herd
                           # settings at all, so this is the only place to move it
                           # and the hooks read the same value.
#HERD_TEMPLATES=~/.herd/templates
#HERD_ERRLOG=~/.herd/hook-errors.log
#HERD_ERRLOG_MAX=1048576   # bytes before rotating to .1; 0 keeps everything
#HERD_TOOL_THROTTLE=2      # seconds to coalesce PostToolUse writes
#HERD_DAEMON_LOG_MAX=1048576
"""
