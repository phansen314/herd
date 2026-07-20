"""herd doctor — one command that answers "why isn't herd recording anything?".

Every failure this reports is one the system is DESIGNED to survive silently:
hooks never print to Claude, a missing dependency exits 0, the daemon logs to a
journal you have to know to read. That is the right behaviour at runtime and a
terrible one to debug, so the diagnosis has to be somewhere — here.

Checks are pure functions returning (level, headline, detail) and take their
inputs explicitly, so the suite can drive every branch without a broken machine.
Nothing here writes: doctor must be safe on a system that is already sick.
"""
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys

from herd import MIN_PYTHON
from herd import daemon
from herd import settings as _settings

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✔", WARN: "!", FAIL: "✘"}

# The binaries the hooks shell out to. kitten is optional — only spawn/jump need
# it, and plenty of herd works without kitty at all.
REQUIRED = ("jq", "sqlite3", "ps", "bash")
OPTIONAL = ("kitten", "fzf")

# strflocaltime, which the statusline formats both reset stamps with. PRESENCE IS
# NOT ENOUGH HERE: on jq 1.5 that function does not exist, the call raises, and a
# raise aborts the WHOLE filter — so all 23 fields come back empty and the
# statusline sinks nothing, silently. The per-field `try` wrappers in statusline.sh
# cannot help; they catch a bad field, not an unknown function.
JQ_MIN = (1, 6)


def _db_path():
    return daemon.DEFAULT_DB


# ── settings.json, defensively ───────────────────────────────────────────────
# EVERY shape below is treated as suspect. This file is hand-edited, written by
# other tools, and half of what doctor exists to diagnose IS a malformed one —
# `b["hooks"]` raised KeyError on a block carrying only a matcher, so the command
# you run to find out why herd is broken died with a traceback instead of naming
# the block. install._strip_managed has always used .get() here; doctor did not.
def _hook_commands(data):
    """Delegates to settings.hook_commands — the walk install uses too."""
    return _settings.hook_commands(data)


def _statusline_command(data):
    """Delegates to settings.statusline_command."""
    return _settings.statusline_command(data)


def _readable(path):
    """(text, problem). Never raises: a file doctor cannot read is a FINDING, and
    the one file it must report on is the one it just failed to open."""
    try:
        return pathlib.Path(path).read_text(), None
    except FileNotFoundError:
        return None, None                       # absent: the caller's own message
    except (OSError, ValueError) as e:          # ValueError covers UnicodeDecodeError
        return None, str(e)


# ── checks (pure) ────────────────────────────────────────────────────────────
def check_deps(which=shutil.which):
    out = []
    for b in REQUIRED:
        p = which(b)
        out.append((OK, f"{b}", p) if p else
                   (FAIL, f"{b} NOT FOUND", "hooks cannot parse or write anything"))
    for b in OPTIONAL:
        p = which(b)
        out.append((OK, f"{b}", p) if p else
                   (WARN, f"{b} not found", "herd spawn / jump need it; the rest works"))
    return out


def check_jq_version(which=shutil.which, run=None):
    """`which jq` answers the wrong question — jq 1.5 is on PATH and still breaks
    the statusline outright (see JQ_MIN). Returns [] when jq is absent, because
    check_deps already FAILs that and two lines for one cause is noise."""
    if which("jq") is None:
        return []
    run = run or (lambda: subprocess.run(["jq", "--version"], capture_output=True,
                                         text=True, timeout=5).stdout)
    try:
        raw = (run() or "").strip()
    except Exception as e:                       # noqa: BLE001
        return [(WARN, "jq version unknown", f"could not run `jq --version`: {e}")]
    # "jq-1.7.1", "jq-1.6", older "jq version 1.5", prereleases "jq-1.7rc1".
    m = re.search(r"(\d+)\.(\d+)", raw)
    if not m:
        return [(WARN, "jq version unreadable", f"`jq --version` said {raw!r}")]
    ver = (int(m.group(1)), int(m.group(2)))
    if ver < JQ_MIN:
        return [(FAIL, f"jq {raw} is too old",
                 f"herd needs jq >= {JQ_MIN[0]}.{JQ_MIN[1]} (strflocaltime) — without "
                 "it the statusline\n      filter aborts and records NO cost, context "
                 "or branch, silently")]
    return [(OK, f"jq {'.'.join(str(n) for n in ver)}", "supports strflocaltime")]


def check_python(version_info=None, executable=None):
    """The dependency doctor is standing inside. bin/herd proves python3 EXISTS
    (`command -v`) and herd/__init__ enforces the floor at import — but neither is
    visible in a report, and 'why isn't herd recording' has been answered by 'that
    is the system python, not the one you installed for' more than once."""
    vi = version_info or sys.version_info
    exe = executable or sys.executable
    cur = (vi[0], vi[1])
    txt = f"{cur[0]}.{cur[1]}"
    if cur < MIN_PYTHON:
        return [(FAIL, f"python {txt} is too old",
                 f"{exe} — herd needs >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")]
    return [(OK, f"python {txt}", exe)]


def check_db(path, connect_fn=None):
    """Exists, has the schema, is writable, and passes a quick integrity check."""
    if not os.path.exists(path):
        return [(FAIL, "database missing", f"{path} — run: python3 -m herd.install")]
    connect_fn = connect_fn or (lambda p: sqlite3.connect(p, timeout=3))
    try:
        c = connect_fn(path)
    except Exception as e:                       # noqa: BLE001
        return [(FAIL, "database unopenable", f"{path}: {e}")]
    out = []
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            out.append((FAIL, "schema not applied", "run: python3 -m herd.install"))
        else:
            live = c.execute("SELECT COUNT(*) FROM sessions "
                             "WHERE stopped_at IS NULL").fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            out.append((OK, "database", f"{path} — {live} live / {total} total"))
        quick = c.execute("PRAGMA quick_check").fetchone()[0]
        out.append((OK, "integrity", "quick_check ok") if quick == "ok" else
                   (FAIL, "database corrupt", str(quick)))
        if not os.access(path, os.W_OK):
            out.append((FAIL, "database not writable",
                        "hooks will exit 0 and record nothing"))
    except Exception as e:                       # noqa: BLE001
        out.append((FAIL, "database unreadable", str(e)))
    finally:
        c.close()
    return out


def check_wiring(settings_text, hook_roots, statusline_paths, events):
    """Are herd's hooks and statusline actually wired into settings.json, and do
    the wired paths still exist?

    hook_roots / statusline_paths are the ACCEPTABLE locations, in preference
    order: the installed copy (~/.herd/hooks) and the checkout (a --dev install).
    Accepting either is what lets doctor report the mode rather than mistaking a
    deliberate --dev install for broken wiring."""
    if settings_text is None:
        return [(FAIL, "settings.json missing", "run: python3 -m herd.install")]
    try:
        data = json.loads(settings_text)
    except ValueError as e:
        return [(FAIL, "settings.json unparseable", str(e))]

    out = []
    wired = {}
    for ev, cmd in _hook_commands(data):
        wired.setdefault(ev, []).append(cmd)
    for event in events:
        # settings.is_managed, NOT a substring test against the current roots. The
        # broader match is deliberate (see settings.py): a prefix test misses an
        # install made from a checkout that has since MOVED — which is precisely the
        # case install._is_managed exists to handle, and which doctor used to report
        # as `<event> not wired` about hooks that were running fine. Two definitions
        # of ownership, one file apart; now one.
        cmds = [c for c in wired.get(event, [])
                if _settings.is_managed(c, hook_roots)]
        if not cmds:
            out.append((FAIL, f"{event} not wired", "run: python3 -m herd.install"))
        elif not os.path.exists(cmds[0]):
            out.append((FAIL, f"{event} points at a missing file", cmds[0]))
        elif not os.access(cmds[0], os.X_OK):
            out.append((FAIL, f"{event} hook is not executable",
                        f"chmod +x {cmds[0]} — a lost +x is a silent no-op"))
        else:
            out.append((OK, event, cmds[0]))

    sl = _statusline_command(data)
    if not sl:
        out.append((FAIL, "statusLine not set",
                    "no cost / context / branch will ever be recorded"))
        return out
    if sl in statusline_paths or any(str(r) in sl for r in hook_roots):
        out.append((OK, "statusLine", sl))
        return out
    # The wrapper read is the one place doctor opens a file whose contents it does
    # not control. A statusLine naming a DIRECTORY, an unreadable file, or one that
    # is not UTF-8 all raised straight out of here — verified.
    wrapper, problem = _readable(sl) if os.path.exists(sl) else (None, None)
    if problem:
        out.append((WARN, "statusLine unreadable", f"{sl}: {problem}"))
    elif wrapper is not None and any(s in wrapper for s in statusline_paths):
        out.append((OK, "statusLine (via wrapper)", sl))
    else:
        out.append((WARN, "statusLine is not herd's", f"{sl} — herd records no metrics"))
    return out


def check_hook_mode(settings_text, installed_root, checkout_root, current=None):
    """Which hooks are actually running, and are they current?

    A --dev install wires the checkout, so a `git checkout`, stash or rebase
    changes what every live Claude session executes — that is a legitimate choice
    while developing hooks, but it should never be a surprise. A copy install is
    stable, and gains the opposite failure: edits to the tree do nothing until you
    re-install. Both are quiet, so both are reported."""
    if settings_text is None:
        return [(FAIL, "hook mode unknown", "settings.json missing")]
    try:
        data = json.loads(settings_text)
    except ValueError:
        return [(FAIL, "hook mode unknown", "settings.json unparseable")]
    cmds = [c for _, c in _hook_commands(data)]
    on_checkout = [c for c in cmds if str(checkout_root) in c]
    on_installed = [c for c in cmds if str(installed_root) in c]

    if on_checkout and not on_installed:
        return [(WARN, "hooks run from the CHECKOUT (--dev)",
                 f"{checkout_root}\n      a git checkout/stash changes what running "
                 "sessions execute")]
    if on_checkout and on_installed:
        return [(FAIL, "hooks wired to BOTH the checkout and the installed copy",
                 "some events fire twice — re-run the installer")]
    if not on_installed:
        return [(FAIL, "no herd hooks wired", "run: python3 -m herd.install")]
    fresh = current if current is not None else _hooks_current()
    if fresh is None:
        return [(OK, "hooks run from the installed copy", str(installed_root))]
    if fresh:
        return [(OK, "hooks run from the installed copy", f"{installed_root} (current)")]
    return [(WARN, "the installed hooks are STALE",
             f"{installed_root} differs from the checkout — "
             "re-run python3 -m herd.install to pick up your edits")]


def _hooks_current():
    from herd import install
    try:
        return install.hooks_are_current()
    except OSError:
        return None


def check_daemon(lock_path, holder=None, alive=None):
    """The daemon is the only reaper of silent deaths, so 'not running' shows up
    as sessions that never leave `herd ls` — never as an error."""
    alive = alive if alive is not None else _pid_alive
    if not os.path.exists(lock_path):
        return [(FAIL, "daemon not running", "sessions will never leave `herd ls`")]
    pid = holder if holder is not None else daemon.holder_pid(lock_path)
    if pid and alive(pid):
        return [(OK, "daemon", f"running (pid {pid})")]
    return [(FAIL, "daemon not running",
             f"stale lock at {lock_path} — start it, or: {_service_start_hint()}")]


def _service_start_hint():
    """The service manager herd installed the daemon under differs by platform, and
    handing a macOS user a systemctl line is the kind of advice that reads as herd
    being broken rather than the hint being wrong."""
    from herd import install                            # local, as in _hooks_current
    if sys.platform == "darwin":
        return f"launchctl kickstart gui/{os.getuid()}/{install.LAUNCHD_LABEL}"
    return "systemctl --user start herd"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def check_config(environ, path=None, daemon_env=None):
    """The config file, and whether the running daemon actually got it.

    Two failures live here, and only the second is obvious. A malformed line is
    reported by the parser. The quiet one is DIVERGENCE: the hooks are children of
    your shell and the systemd daemon inherits nothing from it, so a key set in one
    place and not the other is obeyed by half of herd. That is not hypothetical —
    HERD_CLAUDE_NAME exported in .bashrc made the hooks store a pid the reaper then
    read as recycled, stopping every live session on its first tick. Reading the
    live daemon's /proc environ is the only way to state what it is REALLY running
    with, rather than what this process happens to see."""
    from herd import config as herd_config
    out = []
    values, problems = herd_config.load(path)
    for msg in problems:
        out.append((WARN, "config", msg))
    if not values and not problems:
        return out                              # no file, nothing to say

    for key, val in sorted(values.items()):
        cur = environ.get(key)
        if cur is not None and cur != val:
            out.append((WARN, f"{key} overridden by the environment",
                        f"config says {val!r}, this process has {cur!r}"))
        else:
            out.append((OK, key, val))

    # The daemon is the reader that CANNOT be checked from here by inference: it is
    # a different process with a different environment. Ask it.
    env = daemon_env if daemon_env is not None else _daemon_environ()
    if env is None:
        return out
    for key, val in sorted(values.items()):
        got = env.get(key)
        if got is None:
            out.append((FAIL, f"the running daemon does not have {key}",
                        "restart it to pick up the config file "
                        "(systemctl --user restart herd)"))
        elif got != val:
            out.append((WARN, f"the running daemon has a different {key}",
                        f"config says {val!r}, the daemon has {got!r} — "
                        "its unit or its shell set it"))
    return out


def _daemon_environ(lock_path=None, read=None):
    """The live daemon's environment, or None when it cannot be read (not running,
    not Linux, or a daemon owned by another user). Linux-only by design: /proc is
    the only place a process's real environment is legible, and everywhere else
    this check simply stays quiet rather than guessing."""
    holder = None
    try:
        p = pathlib.Path(lock_path or daemon.lock_path())
        holder = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    reader = read or (lambda pid: pathlib.Path(f"/proc/{pid}/environ").read_bytes())
    try:
        raw = reader(holder)
    except OSError:
        return None
    env = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            k, v = chunk.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def check_env(environ):
    """A malformed threshold used to traceback every command; now it silently falls
    back to the default, so say which ones are being ignored."""
    out = []
    # Every knob _int_env validates, plus the two log caps — the list had drifted
    # from the daemon's, so a malformed HERD_DAEMON_LOG_MAX (which daemon._int_env
    # does reject) was reported by nobody, and the docstring's promise was only
    # two-thirds true.
    for name in ("HERD_WAIT_SECS", "HERD_APPROVAL_SECS", "HERD_STUCK_SECS",
                 "HERD_STRANDED_SECS", "HERD_TOOL_THROTTLE",
                 "HERD_DAEMON_LOG_MAX", "HERD_ERRLOG_MAX"):
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            val = int(raw)
        except ValueError:
            out.append((WARN, f"{name} is not an integer", f"{raw!r} — using the default"))
            continue
        # Kept in step with daemon._int_env, which rejects these: a negative grace
        # period is a cutoff in the FUTURE, and doctor reporting it as OK while the
        # daemon ignores it is the worst of both.
        if val < 0:
            out.append((WARN, f"{name} is negative", f"{raw!r} — using the default"))
        else:
            out.append((OK, name, raw))

    # HERD_CLAUDE_NAME is not a threshold, and its failure is the loudest of any
    # knob here: `ps -o comm=` reads /proc/pid/stat, capped at 15 chars, so a
    # longer name can never match what the reaper observes and _dead reads the
    # mismatch as a recycled pid — every live session reaped on the first tick.
    # The reaper truncates to compare (daemon._is_claude), so this is a warning
    # about a name that no longer breaks anything, not a failure.
    name = environ.get("HERD_CLAUDE_NAME")
    if name:
        if len(name) > daemon._COMM_MAX:
            out.append((WARN, "HERD_CLAUDE_NAME is longer than ps reports",
                        f"{name!r} — matched on its first {daemon._COMM_MAX} chars "
                        f"({name[:daemon._COMM_MAX]!r})"))
        else:
            out.append((OK, "HERD_CLAUDE_NAME", name))

    # Not an integer and not a name: the tier-2 switch. Off is a legitimate choice
    # (core-only collection), but "herd_attention is empty" has an obvious cause.
    if environ.get("HERD_ATTENTION", "").strip().lower() in ("0", "false", "no", "off"):
        out.append((WARN, "HERD_ATTENTION is off",
                    "core-only: the reaper runs, nothing is ever marked for attention"))
    return out or [(OK, "env", "no HERD_* overrides")]


def check_errlog(path, tail=3, read=None):
    """The hooks' only voice. Empty is good news; recent entries are the answer to
    most 'herd isn't recording' questions."""
    read = read or (lambda p: pathlib.Path(p).read_text())
    if not os.path.exists(path):
        return [(OK, "hook errors", "none logged")]
    try:
        lines = [ln for ln in read(path).splitlines() if ln.strip()]
    # ValueError, not just OSError: a hook killed mid-write (the statusline is
    # killed on timeout as a matter of course) can leave bytes that are not UTF-8,
    # and UnicodeDecodeError escaped this handler — from the check reporting on
    # "the hooks' only voice".
    except (OSError, ValueError) as e:
        return [(WARN, "hook error log unreadable", str(e))]
    if not lines:
        return [(OK, "hook errors", "none logged")]
    level = FAIL if any("NOT FOUND" in ln for ln in lines) else WARN
    return [(level, f"{len(lines)} hook error(s)", "\n      ".join(lines[-tail:]))]


# ── driver ───────────────────────────────────────────────────────────────────
def _safe(label, fn, *a, **kw):
    """Run a check; turn an unexpected exception into a FINDING, not a traceback.

    The specific crash paths are fixed at their source, but this is the property
    that matters and it should not depend on having thought of every input: doctor
    runs on a machine that is already sick, by someone who has just been told
    nothing is being recorded. A traceback there costs them the other five
    sections' worth of diagnosis too.
    """
    try:
        return fn(*a, **kw)
    except Exception as e:                       # noqa: BLE001 — that is the point
        return [(FAIL, f"{label} check crashed",
                 f"{type(e).__name__}: {e} — this is a herd bug, please report it")]


def check_kitty(environ, which=shutil.which, run=None):
    """Is kitty's remote control actually available to herd?

    Returns [] when kitten is absent — check_deps already WARNs it, and two lines
    for one cause is noise (same rule as check_jq_version).

    This reads the ENVIRONMENT, never kitty.conf: that file has includes and
    last-wins overrides, so a parse could confidently declare a working setup
    broken. See herd.kitty.config for the full argument. WARN and never FAIL —
    kitten/fzf are OPTIONAL and herd still records sessions without kitty; only
    placement, spawn and jump are lost.
    """
    from herd.kitty import config
    if which("kitten") is None:
        return []
    st = config.state(environ)
    if st == config.NOT_KITTY:
        # Unverifiable, not broken. check_hook_mode's unknown-state branch is the
        # precedent: a check that cries wolf where it cannot see teaches people to
        # ignore it.
        return [(OK, "kitty remote control unverified",
                 "not running inside kitty — run `herd doctor` in a kitty window")]
    if st == config.OFF:
        return [(WARN, "kitty remote control is OFF",
                 "in kitty, but KITTY_LISTEN_ON is unset, so herd records no window "
                 "for any\n      session and spawn/jump cannot work. Add to "
                 f"{config.KITTY_CONF}:\n"
                 "        allow_remote_control yes\n"
                 "        listen_on unix:/tmp/kitty-{kitty_pid}\n"
                 f"      then {config.RESTART}")]
    sock = environ["KITTY_LISTEN_ON"]
    # focus._ls() is deliberately not reused: it collapses timeout, missing binary
    # and dead socket into "", and the whole point here is to report WHICH.
    run = run or (lambda: subprocess.run(["kitten", "@", "--to", sock, "ls"],
                                         capture_output=True, text=True, timeout=5))
    try:
        p = run()
    except Exception as e:                       # noqa: BLE001
        return [(WARN, "kitty remote control unreachable",
                 f"KITTY_LISTEN_ON={sock} but `kitten @ ls` failed: {e}")]
    if getattr(p, "returncode", 1) != 0:
        why = (getattr(p, "stderr", "") or "").strip().splitlines()
        return [(WARN, "kitty remote control refused",
                 f"{sock}: {why[-1] if why else 'kitten @ ls exited nonzero'}")]
    return [(OK, "kitty remote control", sock)]


def collect(environ=None, settings_path=None):
    """Run every check. Returns [(section, [(level, headline, detail), ...])]."""
    from herd import install                      # local: pulls in pathlib/HOME only
    environ = environ if environ is not None else os.environ
    settings = pathlib.Path(settings_path or install.SETTINGS)
    # A settings.json that EXISTS but cannot be read is its own finding — read_text
    # raised straight out of collect (PermissionError, verified), and an unreadable
    # settings.json is a first-class reason herd records nothing.
    text, problem = _readable(settings)
    # When it is unreadable, that finding REPLACES the two checks that parse it —
    # both would only report "settings.json missing", which is a different problem
    # with a different fix and contradicts the line above it.
    wiring = ([(FAIL, "settings.json unreadable", f"{settings}: {problem}")] if problem
              else None)
    errlog = environ.get("HERD_ERRLOG",
                         str(pathlib.Path.home() / ".herd" / "hook-errors.log"))
    roots = (install.INSTALLED_HOOKS, install.HOOKS_DIR)
    statuslines = tuple(install.statusline_cmd(r) for r in roots)
    return [
        ("dependencies", _safe("dependency", check_deps)
                         + _safe("jq version", check_jq_version)
                         + _safe("python", check_python)),
        ("database", _safe("database", check_db, _db_path())),
        ("wiring", wiring if wiring is not None else
                   _safe("wiring", check_wiring, text, roots, statuslines,
                         tuple(install.HERD_HOOKS))
                   + _safe("hook mode", check_hook_mode, text,
                           install.INSTALLED_HOOKS, install.HOOKS_DIR)),
        ("kitty", _safe("kitty", check_kitty, environ)),
        ("daemon", _safe("daemon", check_daemon, daemon.lock_path())),
        ("config", _safe("config", check_config, environ)),
        ("environment", _safe("environment", check_env, environ)),
        ("hook errors", _safe("hook error log", check_errlog, errlog)),
    ]


def report(sections, out=print):
    """Render, and return an exit code: nonzero when anything FAILed."""
    worst = OK
    for name, results in sections:
        # A section with nothing to say prints nothing. "config" is empty in the
        # common case (no file), and a bare header reads like a check that failed to
        # run rather than one with no findings.
        if not results:
            continue
        out(f"\n  {name}")
        for level, headline, detail in results:
            out(f"    {_MARK[level]} {headline}" + (f"  —  {detail}" if detail else ""))
            if level == FAIL:
                worst = FAIL
            elif level == WARN and worst == OK:
                worst = WARN
    out("")
    if worst == FAIL:
        out("  herd is not healthy — fix the ✘ lines above.")
        return 1
    out("  herd looks healthy." if worst == OK else "  herd works, with warnings.")
    return 0


_FLAGS = {"--help", "-h"}

USAGE = """usage: herd doctor

  Diagnoses why herd is not recording: dependencies, database, wiring, kitty
  remote control, daemon, environment overrides, and the hook error log.
  Writes nothing.

  Exit: 0 healthy (or warnings only), 1 when anything FAILED."""


def main(argv=None):
    """Unknown argv is REFUSED, as in install/daemon/cli. `herd doctor --help` ran
    a full diagnostic and `herd doctor --json` silently did the same — a flag this
    command cannot read means the caller wanted output it is not producing."""
    argv = argv if argv is not None else sys.argv[1:]
    unknown = [a for a in argv if a not in _FLAGS]
    if unknown:
        print(f"herd doctor: unknown option {', '.join(repr(a) for a in unknown)}")
        print()
        print(USAGE)
        return 2
    if argv:
        print(USAGE)
        return 0
    return report(collect())
