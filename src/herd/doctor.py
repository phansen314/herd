"""herd doctor — one command that answers "why isn't herd recording anything?".

Every failure this reports is one the system is DESIGNED to survive silently:
hooks never print to Claude, a missing dependency exits 0, the daemon logs to a
journal you have to know to read. The diagnosis has to live somewhere — here.

Checks are pure functions returning (level, headline, detail) and take their inputs
explicitly, so the suite can drive every branch without a broken machine. Nothing
here writes: doctor must be safe on a system that is already sick.
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

# strflocaltime, which the statusline formats both reset stamps with. Presence of
# jq is not enough: on 1.5 that function does not exist, and the raise aborts the
# WHOLE filter, so all 23 fields come back empty. statusline.sh's per-field `try`
# wrappers cannot help — they catch a bad field, not an unknown function.
JQ_MIN = (1, 6)


def _db_path():
    return daemon.DEFAULT_DB


# ── settings.json, defensively ───────────────────────────────────────────────
# EVERY shape below is suspect: the file is hand-edited, written by other tools,
# and half of what doctor exists to diagnose IS a malformed one.
def _hook_commands(data):
    """The same walk install uses."""
    return _settings.hook_commands(data)


def _statusline_command(data):
    return _settings.statusline_command(data)


def _readable(path):
    """(text, problem). Never raises: a file doctor cannot read is a FINDING."""
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
    """`which jq` answers the wrong question — 1.5 is on PATH and still breaks the
    statusline (see JQ_MIN). Returns [] when jq is absent: check_deps already FAILs
    that, and two lines for one cause is noise."""
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
    """The dependency doctor is standing inside. bin/herd and herd/__init__ already
    enforce the floor, but neither is visible in a report — and "that is the system
    python, not the one you installed for" is a recurring answer here."""
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
    """Are herd's hooks and statusline wired into settings.json, and do the wired
    paths still exist?

    hook_roots / statusline_paths are the ACCEPTABLE locations, in preference
    order: the installed copy (~/.herd/hooks) and the checkout (a --dev install).
    Accepting either is what stops a deliberate --dev install reading as broken."""
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
        # settings.is_managed, NOT a substring test against the current roots: a
        # prefix test misses an install made from a checkout that has since MOVED
        # and reports working hooks as `<event> not wired`. See settings.py.
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
    # The one place doctor opens a file whose contents it does not control: a
    # statusLine may name a directory, an unreadable file, or non-UTF-8 bytes.
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

    A --dev install wires the checkout, so a checkout/stash/rebase changes what
    every live session executes; a copy install has the opposite failure, where
    edits do nothing until you re-install. Both are quiet, so both are reported."""
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
    """The daemon is the only reaper of silent deaths, so "not running" shows up as
    sessions that never leave `herd ls`, never as an error."""
    alive = alive if alive is not None else _pid_alive
    if not os.path.exists(lock_path):
        return [(FAIL, "daemon not running", "sessions will never leave `herd ls`")]
    pid = holder if holder is not None else daemon.holder_pid(lock_path)
    if pid and alive(pid):
        return [(OK, "daemon", f"running (pid {pid})")]
    return [(FAIL, "daemon not running",
             f"stale lock at {lock_path} — start it, or: {_service_start_hint()}")]


def _service_start_hint():
    """The service manager differs by platform; a systemctl line on macOS reads as
    herd being broken rather than the hint being wrong."""
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

    The quiet failure is DIVERGENCE: the hooks are children of your shell and the
    systemd daemon inherits nothing from it, so a key set in one place and not the
    other is obeyed by half of herd. Reading the live daemon's /proc environ is the
    only way to see what it is really running with."""
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

    # The daemon cannot be checked by inference — different process, different
    # environment. Ask it.
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
    not Linux, or owned by another user). Linux-only by design: /proc is the only
    place a process's real environment is legible; elsewhere this stays quiet."""
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
    """A malformed threshold silently falls back to the default, so say which ones
    are being ignored."""
    out = []
    # Must stay in step with every knob daemon._int_env validates, plus the two log
    # caps — a knob missing here is one nobody reports.
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
        # daemon._int_env rejects these too: a negative grace period is a cutoff in
        # the FUTURE, and reporting it OK while the daemon ignores it is the worst
        # of both.
        if val < 0:
            out.append((WARN, f"{name} is negative", f"{raw!r} — using the default"))
        else:
            out.append((OK, name, raw))

    # `ps -o comm=` reads /proc/pid/stat, capped at 15 chars, so a longer name can
    # never match what the reaper observes. daemon._is_claude truncates to compare,
    # so this is a WARN about a name that no longer breaks anything.
    name = environ.get("HERD_CLAUDE_NAME")
    if name:
        if len(name) > daemon._COMM_MAX:
            out.append((WARN, "HERD_CLAUDE_NAME is longer than ps reports",
                        f"{name!r} — matched on its first {daemon._COMM_MAX} chars "
                        f"({name[:daemon._COMM_MAX]!r})"))
        else:
            out.append((OK, "HERD_CLAUDE_NAME", name))

    # The tier-2 switch. Off is legitimate (core-only collection), but it is the
    # obvious cause of "herd_attention is empty".
    if environ.get("HERD_ATTENTION", "").strip().lower() in ("0", "false", "no", "off"):
        out.append((WARN, "HERD_ATTENTION is off",
                    "core-only: the reaper runs, nothing is ever marked for attention"))
    return out or [(OK, "env", "no HERD_* overrides")]


def check_errlog(path, tail=3, read=None):
    """The hooks' only voice. Empty is good news; recent entries answer most
    "herd isn't recording" questions."""
    read = read or (lambda p: pathlib.Path(p).read_text())
    if not os.path.exists(path):
        return [(OK, "hook errors", "none logged")]
    try:
        lines = [ln for ln in read(path).splitlines() if ln.strip()]
    # ValueError, not just OSError: a hook killed mid-write (the statusline is
    # killed on timeout routinely) can leave bytes that are not UTF-8.
    except (OSError, ValueError) as e:
        return [(WARN, "hook error log unreadable", str(e))]
    if not lines:
        return [(OK, "hook errors", "none logged")]
    level = FAIL if any("NOT FOUND" in ln for ln in lines) else WARN
    return [(level, f"{len(lines)} hook error(s)", "\n      ".join(lines[-tail:]))]


# ── driver ───────────────────────────────────────────────────────────────────
def _safe(label, fn, *a, **kw):
    """Run a check; turn an unexpected exception into a FINDING, not a traceback.
    The property must not depend on having thought of every input: a traceback here
    costs the user the other seven sections' worth of diagnosis."""
    try:
        return fn(*a, **kw)
    except Exception as e:                       # noqa: BLE001 — that is the point
        return [(FAIL, f"{label} check crashed",
                 f"{type(e).__name__}: {e} — this is a herd bug, please report it")]


def check_kitty(environ, which=shutil.which, run=None):
    """Is kitty's remote control actually available to herd?

    Returns [] when kitten is missing — check_deps already WARNs it (same rule as
    check_jq_version). Reads the ENVIRONMENT, never kitty.conf: that file has
    includes and last-wins overrides, so a parse could confidently declare a
    working setup broken (see herd.kitty.config). WARN and never FAIL — without
    kitty only placement, spawn and jump are lost.
    """
    from herd.kitty import config
    if which("kitten") is None:
        return []
    st = config.state(environ)
    if st == config.NOT_KITTY:
        # Unverifiable, not broken — a check that cries wolf where it cannot see
        # teaches people to ignore it. Same as check_hook_mode's unknown branch.
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
    # and dead socket into "", and the point here is to report WHICH.
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
    # A settings.json that EXISTS but cannot be read is its own finding.
    text, problem = _readable(settings)
    # That finding REPLACES the two checks that parse it: both would only report
    # "settings.json missing", a different problem with a different fix.
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
        # A section with nothing to say prints nothing: "config" is empty in the
        # common case, and a bare header reads like a check that failed to run.
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
    """Unknown argv is REFUSED, as in install/daemon/cli: a flag this command
    cannot read means the caller wanted output it is not producing."""
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
