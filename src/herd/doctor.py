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
import shutil
import sqlite3

from herd import daemon

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✔", WARN: "!", FAIL: "✘"}

# The binaries the hooks shell out to. kitten is optional — only spawn/jump need
# it, and plenty of herd works without kitty at all.
REQUIRED = ("jq", "sqlite3", "ps", "bash")
OPTIONAL = ("kitten", "fzf")


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


def check_wiring(settings_text, hooks_dir, statusline_path, events):
    """Are herd's hooks and statusline actually wired into settings.json, and do
    the wired paths still exist? A moved checkout leaves absolute paths behind."""
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
        cmds = [c for c in wired.get(event, []) if str(hooks_dir) in c]
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
    elif sl == statusline_path or str(hooks_dir) in sl:
        out.append((OK, "statusLine", sl))
    elif os.path.exists(sl) and statusline_path in pathlib.Path(sl).read_text():
        out.append((OK, "statusLine (via wrapper)", sl))
    else:
        out.append((WARN, "statusLine is not herd's", f"{sl} — herd records no metrics"))
    return out


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
             f"stale lock at {lock_path} — start it, or: systemctl --user start herd")]


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
            int(raw)
            out.append((OK, name, raw))
        except ValueError:
            out.append((WARN, f"{name} is not an integer", f"{raw!r} — using the default"))
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
    return [
        ("dependencies", check_deps()),
        ("database", check_db(_db_path())),
        ("wiring", check_wiring(text, install.HOOKS_DIR, install.STATUSLINE,
                                tuple(install.HERD_HOOKS))),
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
