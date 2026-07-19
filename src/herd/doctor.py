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
    hooks = data.get("hooks") or {}
    wired = {e: [h.get("command", "") for b in hooks.get(e, []) for h in b["hooks"]]
             for e in hooks}
    for event in events:
        cmds = [c for c in wired.get(event, [])
                if any(str(r) in c for r in hook_roots)]
        if not cmds:
            out.append((FAIL, f"{event} not wired", "run: python3 -m herd.install"))
        elif not os.path.exists(cmds[0]):
            out.append((FAIL, f"{event} points at a missing file", cmds[0]))
        elif not os.access(cmds[0], os.X_OK):
            out.append((FAIL, f"{event} hook is not executable",
                        f"chmod +x {cmds[0]} — a lost +x is a silent no-op"))
        else:
            out.append((OK, event, cmds[0]))

    sl = (data.get("statusLine") or {}).get("command", "")
    if not sl:
        out.append((FAIL, "statusLine not set",
                    "no cost / context / branch will ever be recorded"))
    elif sl in statusline_paths or any(str(r) in sl for r in hook_roots):
        out.append((OK, "statusLine", sl))
    elif os.path.exists(sl) and any(s in pathlib.Path(sl).read_text()
                                    for s in statusline_paths):
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
    cmds = [h.get("command", "") for bs in (data.get("hooks") or {}).values()
            for b in bs for h in b["hooks"]]
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


def check_env(environ):
    """A malformed threshold used to traceback every command; now it silently falls
    back to the default, so say which ones are being ignored."""
    out = []
    for name in ("HERD_WAIT_SECS", "HERD_APPROVAL_SECS", "HERD_STUCK_SECS",
                 "HERD_STRANDED_SECS", "HERD_TOOL_THROTTLE"):
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
    return out or [(OK, "env", "no HERD_* overrides")]


def check_errlog(path, tail=3, read=None):
    """The hooks' only voice. Empty is good news; recent entries are the answer to
    most 'herd isn't recording' questions."""
    read = read or (lambda p: pathlib.Path(p).read_text())
    if not os.path.exists(path):
        return [(OK, "hook errors", "none logged")]
    try:
        lines = [ln for ln in read(path).splitlines() if ln.strip()]
    except OSError as e:
        return [(WARN, "hook error log unreadable", str(e))]
    if not lines:
        return [(OK, "hook errors", "none logged")]
    level = FAIL if any("NOT FOUND" in ln for ln in lines) else WARN
    return [(level, f"{len(lines)} hook error(s)", "\n      ".join(lines[-tail:]))]


# ── driver ───────────────────────────────────────────────────────────────────
def collect(environ=None, settings_path=None):
    """Run every check. Returns [(section, [(level, headline, detail), ...])]."""
    from herd import install                      # local: pulls in pathlib/HOME only
    environ = environ if environ is not None else os.environ
    settings = pathlib.Path(settings_path or install.SETTINGS)
    text = settings.read_text() if settings.exists() else None
    errlog = environ.get("HERD_ERRLOG",
                         str(pathlib.Path.home() / ".herd" / "hook-errors.log"))
    roots = (install.INSTALLED_HOOKS, install.HOOKS_DIR)
    statuslines = tuple(install.statusline_cmd(r) for r in roots)
    return [
        ("dependencies", check_deps() + check_jq_version() + check_python()),
        ("database", check_db(_db_path())),
        ("wiring", check_wiring(text, roots, statuslines, tuple(install.HERD_HOOKS))
                   + check_hook_mode(text, install.INSTALLED_HOOKS, install.HOOKS_DIR)),
        ("daemon", check_daemon(daemon.lock_path())),
        ("environment", check_env(environ)),
        ("hook errors", check_errlog(errlog)),
    ]


def report(sections, out=print):
    """Render, and return an exit code: nonzero when anything FAILed."""
    worst = OK
    for name, results in sections:
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


def main(argv=None):
    return report(collect())
