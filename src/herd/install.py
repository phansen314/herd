"""herd installer — cut over from klawde (capture only).

    python3 -m herd.install            # wire herd, unwire klawde (backs up first)
    python3 -m herd.install --uninstall  # restore the pre-herd state
    python3 -m herd.install --dry-run    # show what would change, touch nothing

Idempotent. Every edited file is backed up as <file>.herd-bak.<ts> before the
first change, and once as <file>.herd-bak.original — the pre-herd copy uninstall
restores. Nothing is written until the self-test passes; a FAIL aborts and exits
nonzero. Reuses herd.db for the DB bootstrap. Leaves klawde's repo and
~/.klawde/sessions.db (history) in place — only unwires it from settings.json.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from herd.db import connect, apply_schema

HOOKS_DIR = pathlib.Path(__file__).resolve().parent / "hooks"
PKG_SRC = pathlib.Path(__file__).resolve().parent.parent   # .../src (for PYTHONPATH)
REPO = PKG_SRC.parent                                       # repo root
HOME = pathlib.Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
WRAPPER = HOME / ".claude" / "custom-status-line.sh"
HERD_DIR = HOME / ".herd"
DB = HERD_DIR / "herd.db"
SERVICE = HOME / ".config" / "systemd" / "user" / "herd.service"
CLI_SRC = REPO / "bin" / "herd"
CLI_LINK = HOME / ".local" / "bin" / "herd"
COMPLETION_SRC = REPO / "completions" / "herd.bash"
COMPLETION_LINK = HOME / ".local" / "share" / "bash-completion" / "completions" / "herd"

# event -> (hook script, async?). Stop is NEW (klawde has none). SessionStart and
# SessionEnd are BLOCKING; SessionEnd blocking is the fix for klawde's async bug.
HERD_HOOKS = {
    "SessionStart": ("session_start.sh", False),
    "Stop":         ("stop.sh",          True),
    "SessionEnd":   ("session_end.sh",   False),
    "Notification": ("notification.sh",  True),
    "PostToolUse":  ("post_tool_use.sh", True),
}

STATUSLINE = str(HOOKS_DIR / "statusline.sh")

# systemctl can block on a busy/degraded manager; the installer must not hang.
SYSTEMCTL_TIMEOUT = 15
# A wired hook that hangs would hang the self-test that exists to vet it.
SELFTEST_TIMEOUT = 20


def hook_cmd(script):
    return str(HOOKS_DIR / script)


def _is_managed(cmd):
    """A command herd owns — klawde's, or a prior herd install. Everything else
    (cdh, the PreToolUse HTTP hook, anything unknown) is preserved untouched."""
    return "/.klawde/" in cmd or cmd.startswith(str(HOOKS_DIR))


def _ts():
    # no Date.now() concerns here — this is a normal process; use the clock.
    import time
    return time.strftime("%Y%m%d-%H%M%S")


def backup(path, ts):
    if path.exists():
        b = path.with_name(path.name + f".herd-bak.{ts}")
        shutil.copy2(path, b)
        return b
    return None


ORIGINAL_SUFFIX = ".herd-bak.original"


def _is_wired(path, text):
    """Does this file already point at herd? Cheap and textual on purpose — it only
    has to distinguish 'pristine' from 'a previous herd install'."""
    return str(HOOKS_DIR) in text if text else False


def backup_original(path, text):
    """Snapshot the PRE-HERD file, exactly once, under a fixed name.

    The timestamped backups are per-install safety nets and are useless for
    uninstall: the second install backs up the already-wired file, so restoring the
    newest one reinstates herd and reports success. This is the copy uninstall wants
    and it is never overwritten — an already-wired file is not an original."""
    b = path.with_name(path.name + ORIGINAL_SUFFIX)
    if b.exists() or not path.exists() or _is_wired(path, text):
        return None
    shutil.copy2(path, b)
    return b


def _restore_source(path):
    """The backup uninstall should restore: the pristine original when we have one,
    else the OLDEST timestamped backup — on a pre-ORIGINAL_SUFFIX install that is
    the one install #1 took, i.e. the last copy that predates herd. Returns None
    when nothing usable exists."""
    orig = path.with_name(path.name + ORIGINAL_SUFFIX)
    if orig.exists():
        return orig
    baks = sorted(path.parent.glob(path.name + ".herd-bak.*"))
    baks = [b for b in baks if b.name != orig.name]
    return baks[0] if baks else None


def _atomic_write(path, text):
    """Write via tmp + rename in the same directory. settings.json is the one file
    whose truncation stops Claude Code from starting, and write_text() truncates in
    place — a crash or ENOSPC mid-write leaves unparseable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".herd-tmp.{os.getpid()}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ── DB bootstrap ───────────────────────────────────────────────────────────
def bootstrap_db(dry=False):
    if dry:
        return f"would create {DB} and apply core.sql + herd.sql (+ templates dir)"
    HERD_DIR.mkdir(parents=True, exist_ok=True)
    (HERD_DIR / "templates").mkdir(exist_ok=True)   # spawn presets (herd spawn -t)
    conn = connect(str(DB))
    apply_schema(conn)          # idempotent: CREATE TABLE IF NOT EXISTS
    conn.close()
    return f"bootstrapped {DB} + {HERD_DIR / 'templates'}/"


# ── daemon service (reaper + attention) ────────────────────────────────────
# The hooks are bash (run by absolute path); the daemon is python and runs from the
# source tree with PYTHONPATH — no pip install needed, edits picked up on restart.
def _service_python():
    """A ROBUST interpreter for the unit: prefer system python3 over whatever ran
    the installer (often a pyenv shim that needs PATH/init a systemd unit won't have).
    herd is stdlib-only, so any 3.9+ works."""
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(cand, os.X_OK):
            return cand
    return sys.executable


def _has_systemd_user():
    return bool(shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR"))


def service_unit_text():
    """The systemd --user unit. Pure — testable without touching systemd."""
    return (
        "[Unit]\n"
        "Description=herd — Claude Code session tracker (reaper + attention)\n"
        f"Documentation=file://{REPO}\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PYTHONPATH={PKG_SRC}\n"
        f"Environment=HERD_DB={DB}\n"
        f"ExecStart={_service_python()} -m herd.daemon\n"
        # always, not on-failure: a clean exit still means nothing is reaping silent
        # deaths. StartLimitIntervalSec=0 disables the burst limit — a persistent
        # fault (corrupt DB, full disk) must keep retrying rather than latch the
        # unit into `failed`, where the only symptom is sessions that never leave.
        "Restart=always\n"
        "RestartSec=5\n"
        "StartLimitIntervalSec=0\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_service(dry=False):
    """Write + enable + (re)start the daemon unit. Idempotent. Graceful no-op where
    systemd --user is unavailable (macOS/headless) — herd still works, just run the
    daemon yourself."""
    if not _has_systemd_user():
        return ("daemon service SKIPPED — no systemctl --user here. Run the daemon "
                "yourself:  PYTHONPATH=src python3 -m herd.daemon  (or a launchd job)")
    if dry:
        return f"would write {SERVICE} and enable + (re)start herd.service"
    SERVICE.parent.mkdir(parents=True, exist_ok=True)
    SERVICE.write_text(service_unit_text())
    for args in (["daemon-reload"], ["enable", "herd.service"], ["restart", "herd.service"]):
        subprocess.run(["systemctl", "--user", *args], check=False,
                       capture_output=True, timeout=SYSTEMCTL_TIMEOUT)
    active = subprocess.run(["systemctl", "--user", "is-active", "herd.service"],
                            capture_output=True, text=True,
                            timeout=SYSTEMCTL_TIMEOUT).stdout.strip()
    return f"herd.service written + enabled ({active or 'unknown'})"


def uninstall_service():
    if not _has_systemd_user() or not SERVICE.exists():
        return "no herd.service to remove"
    subprocess.run(["systemctl", "--user", "disable", "--now", "herd.service"],
                   check=False, capture_output=True, timeout=SYSTEMCTL_TIMEOUT)
    SERVICE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False,
                   capture_output=True, timeout=SYSTEMCTL_TIMEOUT)
    return f"removed {SERVICE}"


# ── CLI on PATH + bash completion ──────────────────────────────────────────
def _relink(link, target):
    """Idempotently point `link` at `target` (symlink). mkdir parents."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def _on_path(d):
    return str(d) in os.environ.get("PATH", "").split(os.pathsep)


def install_cli(dry=False):
    """Put `herd` on PATH (~/.local/bin) + install bash completion. Idempotent."""
    if dry:
        return f"would symlink {CLI_LINK} -> {CLI_SRC} + bash completion"
    _relink(CLI_LINK, CLI_SRC)
    _relink(COMPLETION_LINK, COMPLETION_SRC)
    note = f"herd -> {CLI_LINK} + completion"
    if not _on_path(CLI_LINK.parent):
        note += f"  (WARN: {CLI_LINK.parent} not on PATH — add it)"
    return note + ".  Open shells: `hash -r` or a new shell to pick up `herd`."


def uninstall_cli():
    removed = []
    for link, target in ((CLI_LINK, CLI_SRC), (COMPLETION_LINK, COMPLETION_SRC)):
        if link.is_symlink() and link.resolve() == target.resolve():
            link.unlink()
            removed.append(link.name)
    return f"removed CLI links: {removed}" if removed else "no herd CLI links to remove"


# ── optional: Claude terminal-bell notifications (kitty tab bell) ───────────
# herd does NOT own this — it's a Claude-level, terminal-specific preference. So
# the installer OFFERS it (interactive, opt-in, defaults to no) and never forces it:
# on a non-tty it just prints a tip, and it never overrides an existing choice.
def _bell_decision(current, answer):
    """Pure: the channel to set, or None to leave unchanged. Respects any existing
    preferredNotifChannel; otherwise sets terminal_bell only on an affirmative."""
    if current:
        return None
    return "terminal_bell" if answer.strip().lower() in ("y", "yes") else None


def _offer_bell(new_settings):
    """Interactively offer terminal-bell notifications; mutate new_settings if
    accepted. Returns a status line."""
    cur = new_settings.get("preferredNotifChannel")
    if cur:
        return f"preferredNotifChannel already set ({cur!r}) — left as-is"
    if not sys.stdin.isatty():
        return ('kitty tab bell: set "preferredNotifChannel":"terminal_bell" in '
                "settings.json to enable (README, Notifications)")
    ch = _bell_decision(None, input(
        "  Enable kitty tab-bell notifications? Claude rings the bell when a session\n"
        "  wants you (sets preferredNotifChannel=terminal_bell; see README) [y/N]: "))
    if ch:
        new_settings["preferredNotifChannel"] = ch
        return "terminal_bell notifications enabled"
    return "terminal_bell notifications skipped (enable later via README)"


# ── settings.json surgery ──────────────────────────────────────────────────
def _statusline_cmd(data):
    sl = data.get("statusLine")
    return sl.get("command", "") if isinstance(sl, dict) else ""


def statusline_plan(data, wrapper_exists):
    """What settings.statusLine needs, as one of:

    'wrapper' — an existing custom-status-line.sh already sits in front of it, and
                rewire_wrapper points that at herd. Leave the key alone.
    'set'     — absent, klawde's, or already ours: wire herd's statusline directly.
    'foreign' — someone else's statusline. Never clobber it; say so instead.

    Pure, so install() can report it without re-deriving the decision. The 'set'
    case is the one that used to be missing entirely: statusline wiring happened
    ONLY through the wrapper, so a machine without one (anybody who wasn't already
    a klawde user) got no statusline at all while install still printed PASS —
    and statusline.sh is the only writer of every metric column."""
    cmd = _statusline_cmd(data)
    if not cmd or cmd == STATUSLINE or "/.klawde/" in cmd:
        return "set"
    if cmd == str(WRAPPER):
        return "wrapper" if wrapper_exists else "set"     # dangling pointer -> ours
    return "foreign"


def rewire_settings(data, wrapper_exists=False):
    """Return a NEW settings dict with herd wired and klawde unwired. Pure —
    caller decides whether to write it."""
    data = json.loads(json.dumps(data))          # deep copy
    hooks = data.setdefault("hooks", {})

    # 1. strip every herd-managed command (klawde + any prior herd), drop the
    #    blocks that become empty. cdh / PreToolUse-HTTP / others are untouched.
    for event in list(hooks):
        blocks = hooks[event]
        for block in blocks:
            block["hooks"] = [h for h in block.get("hooks", [])
                              if not _is_managed(h.get("command", ""))]
        kept = [b for b in blocks if b.get("hooks")]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]

    # 2. add herd's authoritative entry for each event, in its OWN block at the
    #    front (so herd runs first; cdh keeps its block).
    for event, (script, is_async) in HERD_HOOKS.items():
        entry = {"type": "command", "command": hook_cmd(script)}
        if is_async:
            entry["async"] = True
        hooks.setdefault(event, []).insert(0, {"hooks": [entry]})

    # 3. the statusline. Preserve any sibling keys (padding etc.) — only the
    #    command is ours to set.
    if statusline_plan(data, wrapper_exists) == "set":
        sl = dict(data["statusLine"]) if isinstance(data.get("statusLine"), dict) else {}
        sl.update({"type": "command", "command": STATUSLINE})
        data["statusLine"] = sl

    return data


# ── statusline wrapper ─────────────────────────────────────────────────────
def rewire_wrapper(text):
    """Swap the klawde statusline invocation for herd's. Idempotent."""
    out = []
    replaced = False
    for line in text.splitlines():
        if ".klawde/statusline.sh" in line or STATUSLINE in line:
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f'{indent}"{STATUSLINE}"')
            replaced = True
        else:
            out.append(line)
    return "\n".join(out) + "\n", replaced


# ── self-test: run the WIRED hook against a temp DB ────────────────────────
def selftest():
    """Prove the wired hooks actually write, using a throwaway DB so the real one
    is untouched.

    Invokes each script the way PRODUCTION does — directly, NOT via `bash <path>`.
    That distinction is the whole point: settings.json and the statusline wrapper
    exec these paths, so a missing +x is a silent no-op. Running them through
    `bash` here would mask exactly the bug that shipped a blank statusline.
    """
    # session_id must satisfy valid_sid() — alphanumerics + hyphens only.
    SID = "herd-selftest-0000-4000-8000-000000000001"
    with tempfile.TemporaryDirectory(prefix="herd-selftest-") as tmp:
        env = dict(os.environ, HERD_DB=f"{tmp}/t.db", HERD_RUNTIME=tmp,
                   HERD_ERRLOG=f"{tmp}/err.log")
        c = connect(f"{tmp}/t.db"); apply_schema(c); c.close()

        not_exec = [p.name for p in sorted(HOOKS_DIR.glob("*.sh"))
                    if not os.access(p, os.X_OK)]
        if not_exec:
            return False, {"not_executable": not_exec}

        try:
            subprocess.run([hook_cmd("session_start.sh")],          # direct exec
                           input=f'{{"session_id":"{SID}","cwd":"/x","model":"m","source":"startup"}}',
                           capture_output=True, text=True, env=env,
                           timeout=SELFTEST_TIMEOUT)
            sl = subprocess.run([STATUSLINE],                        # direct exec
                                input=f'{{"session_id":"{SID}","model":{{"id":"m"}},"cwd":"/x",'
                                      '"context_window":{"used_percentage":10},"cost":{"total_cost_usd":0.5}}',
                                capture_output=True, text=True, env=env,
                                timeout=SELFTEST_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            # A hook that hangs is a FAIL, not a hung installer — and since the
            # self-test now gates the write, this is the difference between
            # refusing to wire a hanging hook and wedging on it.
            return False, {"timed_out": e.cmd[0] if e.cmd else "?"}
        c = connect(f"{tmp}/t.db")
        row = c.execute("SELECT status, context_percent FROM sessions "
                        "WHERE session_id=?", (SID,)).fetchone()
        c.close()
        ok = (row is not None and row["status"] == "working"
              and row["context_percent"] == 10
              and sl.returncode == 0 and sl.stdout.strip() != "")
        return ok, dict(row) if row else {"statusline_rc": sl.returncode,
                                          "statusline_out": sl.stdout.strip()}


def install(dry=False):
    ts = _ts()
    print(f"herd install  (ts={ts})\n")
    print("  " + bootstrap_db(dry))

    # A first-time machine may have no settings.json at all — treat that as empty
    # rather than a traceback. rewire_settings() setdefaults "hooks", backup()
    # no-ops on a missing file, so the absent case needs no other special-casing.
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    plan = statusline_plan(settings, WRAPPER.exists())
    new_settings = rewire_settings(settings, wrapper_exists=WRAPPER.exists())
    wrapper_text, wrap_ok = rewire_wrapper(WRAPPER.read_text()) if WRAPPER.exists() else ("", False)
    sl_note = {
        "set":     f"statusLine -> {STATUSLINE}",
        "wrapper": f"statusLine -> {WRAPPER} (rewired to herd below)",
        "foreign": (f"statusLine LEFT ALONE — it runs {_statusline_cmd(settings)!r}, "
                    "which herd does not own. Point it at\n      "
                    f"{STATUSLINE} yourself, or herd records no cost/context/branch."),
    }[plan]

    if dry:
        print(f"  would back up + rewrite {SETTINGS}")
        print(f"  would back up + rewrite {WRAPPER} (statusline -> herd: {wrap_ok})")
        print(f"  {sl_note}")
        print("  " + install_service(dry=True))
        print("  " + install_cli(dry=True))
        print("  would offer (interactive, opt-in) to enable terminal_bell notifications")
        print("\n  resulting hooks:")
        for e, blocks in new_settings["hooks"].items():
            cmds = [h.get("command") or f"<{h.get('type','?')}:{h.get('url','')}>"
                    for b in blocks for h in b["hooks"]]
            print(f"    {e}: {cmds}")
        return

    # GATE THE WRITE. selftest execs the hooks on disk against a throwaway DB — it
    # depends on nothing we are about to write, so it can run first. Running it
    # afterwards (as this used to) meant a provably broken set of hooks was already
    # wired into the user's global config by the time we found out.
    ok, row = selftest()
    print(f"\n  self-test (wired hooks -> temp DB): {'PASS' if ok else 'FAIL'}  {row}")
    if not ok:
        print("\n  ABORTED — nothing was rewired. The hooks do not work as installed;")
        print("  wiring them would have broken every Claude session silently.")
        return 1

    bell_note = _offer_bell(new_settings)   # interactive opt-in; may set the key before we write
    # Re-read: _offer_bell blocks on input(), and Claude Code writes settings.json
    # (permission grants) while we wait. Merging onto the on-disk copy at write time
    # keeps a grant made during the prompt from being clobbered by our stale read.
    if SETTINGS.exists():
        try:
            fresh = json.loads(SETTINGS.read_text())
            fresh.update(new_settings)
            new_settings = fresh
        except (OSError, json.JSONDecodeError):
            pass                                # unreadable now — go with what we built
    backup_original(SETTINGS, SETTINGS.read_text() if SETTINGS.exists() else "")
    backup(SETTINGS, ts)
    _atomic_write(SETTINGS, json.dumps(new_settings, indent=2) + "\n")
    print(f"  rewired {SETTINGS} (backup: *.herd-bak.{ts})")
    print(f"  {sl_note}")
    if WRAPPER.exists():
        backup_original(WRAPPER, WRAPPER.read_text())
        backup(WRAPPER, ts)
        _atomic_write(WRAPPER, wrapper_text)
        print(f"  rewired {WRAPPER} statusline -> herd")

    print("  " + install_service())
    print("  " + install_cli())
    print("  " + bell_note)
    print("\n  use it (new shell picks up `herd` + completion):")
    print("    herd ls        # live sessions, attention-first, by name")
    print("    herd jump      # fuzzy-pick (fzf) a session and focus its window")
    print("\n  klawde is unwired but NOT deleted — ~/.klawde/sessions.db (history) is kept.")
    print(f"  restore:  python3 -m herd.install --uninstall")
    return 0


def uninstall():
    """Restore each edited file to its PRE-HERD state + remove service & CLI."""
    print("  " + uninstall_service())
    print("  " + uninstall_cli())
    rc = 0
    for path in (SETTINGS, WRAPPER):
        src = _restore_source(path)
        if src:
            _atomic_write(path, src.read_text())
            print(f"  restored {path} from {src.name}")
        elif path.exists():
            rc = 1
            print(f"  NO PRE-HERD BACKUP for {path} — left as-is, edit it by hand")
        else:
            print(f"  no backup found for {path}")
    return rc


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if "--uninstall" in argv:
        return uninstall()
    return install(dry="--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main() or 0)
