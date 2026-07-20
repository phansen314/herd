"""herd config — the settings file the daemon and the hooks BOTH read.

    ~/.herd/config          # KEY=value, # comments, blank lines ignored

The environment is not a shared channel between herd's parts: the hooks descend
from your shell, while `systemctl --user` starts the daemon with nothing from it.
A setting that reaches only one side is a divergence, not a preference — see
DECISIONS.md#env-divergence for the two that cost real sessions.

common.sh parses this file with the same rules; test_source_invariants pins the
two key lists together.

PRECEDENCE: a real environment variable WINS, so a one-off override still works.
A shadowed key is REPORTED rather than dropped — a config line that does nothing
is the bug this module exists to end.
"""
import os
import pathlib

# A file key outside this set is a typo (HERD_WAIT_SEC, HERD_STUCK_SECONDS) and is
# reported rather than ignored: a misspelled knob that stays silent is
# indistinguishable from one being obeyed.
KNOWN = (
    # daemon
    "HERD_ATTENTION", "HERD_WAIT_SECS", "HERD_APPROVAL_SECS", "HERD_STUCK_SECS",
    "HERD_STRANDED_SECS", "HERD_DAEMON_LOG_MAX",
    "HERD_BACKOFF_MAX_SECS", "HERD_ORPHAN_GRACE_SECS",
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

    NEVER /tmp: the filenames under it are predictable and the hooks create them
    with plain redirects, which follow symlinks — on a shared box another user can
    pre-create one as a link and have a hook truncate it. /run/user/<uid> and
    ~/.herd/run are 0700 and ours; /tmp was not.

    ONE definition, because every reader must agree — two answers means two daemon
    locks and two daemons."""
    env = os.environ if env is None else env
    d = env.get("HERD_RUNTIME") or env.get("XDG_RUNTIME_DIR")
    if d:
        return d
    # env["HOME"], not expanduser("~"): expanduser reads the REAL environment, so a
    # caller passing an env dict got this process's home and the parameter silently
    # did nothing. common.sh uses $HOME and the two must land on the same directory.
    home = env.get("HOME") or os.path.expanduser("~")
    d = os.path.join(home, ".herd", "run")
    # Called on the hook hot path: an unconditional makedirs is a syscall per tick.
    if mkdir and not os.path.isdir(d):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)          # makedirs honours umask; be explicit
        except OSError:
            pass                        # unwritable HOME: callers degrade already
    return d


def _strip_inline_comment(val):
    """Cut a trailing `# comment` off a value.

    A '#' opens a comment only at the START of the value or AFTER WHITESPACE.
    Anywhere else it is an ordinary character, so `/srv/repo#2/herd.db` survives —
    cutting at every '#' would be the same silent-wrong-value bug reversed.

    herd_load_config in common.sh implements the same rule; the two are pinned by
    test_bash_and_python_strip_inline_comments_the_same_way."""
    for i, ch in enumerate(val):
        if ch == "#" and (i == 0 or val[i - 1] in " \t"):
            return val[:i].rstrip()
    return val


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
        key, val = key.strip(), _strip_inline_comment(val.strip())
        # `export FOO=bar` is what muscle memory types here, and binding a key
        # named "export FOO" would be the silent no-op this module prevents.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key not in KNOWN:
            problems.append(f"line {n}: unknown key {key!r}")
            continue
        if key in values:
            problems.append(f"line {n}: {key} set twice — the later wins")
        # A LEADING ~ ONLY. Nothing else expands it: this file is not read by a
        # shell, and common.sh assigns the value quoted. No $VAR expansion either —
        # one rule both parsers implement identically beats a drifting shell-alike.
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

    Called once at import from daemon.py, which cli.py imports, so every python
    entry point gets the same settings."""
    env = os.environ if env is None else env
    values, problems = load(path)
    applied, shadowed = {}, {}
    for key, val in values.items():
        if env.get(key) is not None:
            # Set in both places. Not an error — a one-off override is a feature —
            # but never silent: the file says one thing, the process does another.
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

# ── daemon retry and sweeps (seconds) ───────────────────────────────────────
#HERD_BACKOFF_MAX_SECS=60   # ceiling on the wait after consecutive tick
                            # failures; never drops below the tick interval
#HERD_ORPHAN_GRACE_SECS=300 # age before a runtime file whose session has no
                            # row is swept

# ── identity and placement ──────────────────────────────────────────────────
# Set these HERE, not in .bashrc: the hooks would see them and the daemon would
# not, and that divergence stops every live session on the reaper's first tick.
#HERD_CLAUDE_NAME=claude   # process name the pid ancestry walk looks for
#HERD_RUNTIME=             # runtime files + daemon lock. Defaults to
                           # $XDG_RUNTIME_DIR, else ~/.herd/run. Setting this in a
                           # shell only splits the lock and runs two daemons.

# ── paths ───────────────────────────────────────────────────────────────────
#HERD_DB=~/.herd/herd.db   # the only place to move it; both sides read this
#HERD_TEMPLATES=~/.herd/templates
#HERD_ERRLOG=~/.herd/hook-errors.log
#HERD_ERRLOG_MAX=1048576   # bytes before rotating to .1; 0 keeps everything
#HERD_TOOL_THROTTLE=2      # seconds to coalesce PostToolUse writes
#HERD_DAEMON_LOG_MAX=1048576
"""
