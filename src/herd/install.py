"""herd installer — cut over from klawde (capture only).

    python3 -m herd.install            # wire herd, unwire klawde (backs up first)
    python3 -m herd.install --uninstall  # restore the pre-herd state
    python3 -m herd.install --dry-run    # show what would change, touch nothing
    python3 -m herd.install --dev        # wire the CHECKOUT, not the installed copy

Hooks are COPIED to ~/.herd/hooks (with the SQL they read) and settings.json is
wired there, so a git checkout in the source tree cannot change what running
Claude sessions execute. `--dev` wires the checkout directly for hook development;
`herd doctor` reports which mode is active and whether the copy has drifted.

Idempotent. Every edited file is backed up as <file>.herd-bak.<ts> before the
first change, and once as <file>.herd-bak.original — the pre-herd copy uninstall
restores. Nothing is written until the self-test passes; a FAIL aborts and exits
nonzero. Reuses herd.db for the DB bootstrap. Leaves klawde's repo and
~/.klawde/sessions.db (history) in place — only unwires it from settings.json.
"""
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from herd.db import connect, apply_schema

HOOKS_DIR = pathlib.Path(__file__).resolve().parent / "hooks"   # in the checkout
SCHEMA_DIR = pathlib.Path(__file__).resolve().parent / "schema"
PKG_SRC = pathlib.Path(__file__).resolve().parent.parent   # .../src (for PYTHONPATH)
REPO = PKG_SRC.parent                                       # repo root
HOME = pathlib.Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
WRAPPER = HOME / ".claude" / "custom-status-line.sh"
HERD_DIR = HOME / ".herd"
DB = HERD_DIR / "herd.db"

# Where the hooks that actually RUN live, by default. Wiring settings.json at the
# checkout makes every Claude session on the machine execute whatever is in the
# working tree at that instant: a `git checkout`, stash or rebase silently changes
# the behaviour of running sessions (this is how `no such statement: W4_event_log`
# reached a live hook-errors.log), and moving the clone breaks all five hooks at
# once. Installing a COPY decouples the two. `--dev` opts back into the checkout.
INSTALLED_HOOKS = HERD_DIR / "hooks"
INSTALLED_SCHEMA = HERD_DIR / "schema"
SERVICE = HOME / ".config" / "systemd" / "user" / "herd.service"
# macOS has no systemd; the same daemon runs as a per-user LaunchAgent. Reverse-DNS
# label to match the other tools in this account, and it doubles as the launchctl
# handle (`launchctl print gui/$UID/com.codingzen.herd`).
LAUNCHD_LABEL = "com.codingzen.herd"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
# launchd keeps no journal, so the daemon's own stderr is the only record of a
# crash loop. Into ~/.herd, next to the DB it is failing to reap.
DAEMON_OUT = HERD_DIR / "daemon.out.log"
DAEMON_ERR = HERD_DIR / "daemon.err.log"
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

STATUSLINE = str(INSTALLED_HOOKS / "statusline.sh")

# systemctl can block on a busy/degraded manager; the installer must not hang.
SYSTEMCTL_TIMEOUT = 15
# launchctl blocks the same way when launchd is wedged. Same bound, same reason.
LAUNCHCTL_TIMEOUT = 15
# A wired hook that hangs would hang the self-test that exists to vet it.
SELFTEST_TIMEOUT = 20

_OUR_SCRIPTS = {s for s, _ in HERD_HOOKS.values()} | {"statusline.sh", "common.sh"}


def hook_cmd(script, hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / script)


def statusline_cmd(hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / "statusline.sh")


def _is_managed(cmd):
    """A command herd owns — klawde's, or any prior herd install. Everything else
    (cdh, the PreToolUse HTTP hook, anything unknown) is preserved untouched.

    Recognising our own scripts BY NAME matters as much as by path: a prefix test
    against the current roots misses an install made from a checkout that has since
    moved, so the stale entry survives the strip and step 2 adds a second one —
    every hook then fires twice. The herd-shaped-path condition keeps us from
    claiming an unrelated tool's session_start.sh."""
    if not cmd:
        return False
    if "/.klawde/" in cmd:
        return True
    if cmd.startswith(str(HOOKS_DIR)) or cmd.startswith(str(INSTALLED_HOOKS)):
        return True
    path = cmd.split()[0]
    return path.rsplit("/", 1)[-1] in _OUR_SCRIPTS and ("/herd/" in path or "/.herd/" in path)


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
    has to distinguish 'pristine' from 'a previous herd install'.

    BOTH roots, or a copy-mode install reads as pristine and overwrites the
    pre-herd snapshot with a herd-wired one — the uninstall trap again."""
    if not text:
        return False
    return str(HOOKS_DIR) in text or str(INSTALLED_HOOKS) in text


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


# ── hook installation (copy, or point at the checkout with --dev) ──────────
def sync_hooks(dev=False, dry=False):
    """Put the hooks where they will be RUN from, and return that directory.

    Copies the SQL too, and that is not incidental: common.sh resolves
    HERD_WRITES as <hooks>/../schema/writes.sql, so hooks installed without their
    schema find no statements and every write path fails with "no such statement"
    — a herd that records nothing while every hook still exits 0."""
    if dev:
        return HOOKS_DIR
    if dry:
        return INSTALLED_HOOKS
    INSTALLED_HOOKS.mkdir(parents=True, exist_ok=True)
    INSTALLED_SCHEMA.mkdir(parents=True, exist_ok=True)
    for src in sorted(HOOKS_DIR.glob("*.sh")):
        dst = INSTALLED_HOOKS / src.name
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | 0o111)   # +x, or the hook is a silent no-op
    for src in sorted(SCHEMA_DIR.glob("*.sql")):
        shutil.copy2(src, INSTALLED_SCHEMA / src.name)
    return INSTALLED_HOOKS


def hooks_are_current(hooks_dir=None):
    """Do the installed copies match the checkout? False means the tree moved on
    without a re-install — the cost of the copy, and what `herd doctor` reports."""
    hooks_dir = hooks_dir or INSTALLED_HOOKS
    if hooks_dir == HOOKS_DIR:
        return True                              # --dev: the checkout IS the copy
    for src in sorted(HOOKS_DIR.glob("*.sh")):
        dst = hooks_dir / src.name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            return False
    for src in sorted(SCHEMA_DIR.glob("*.sql")):
        dst = INSTALLED_SCHEMA / src.name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            return False
    return True


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
        "After=default.target\n"
        # [Unit], NOT [Service] — systemd silently ignores it in [Service]
        # ("Unknown key name ... ignoring") and keeps the 10s default, so a
        # persistently-failing daemon still latches into `failed`.
        "StartLimitIntervalSec=0\n\n"
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
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _has_launchd():
    """macOS. `launchctl` alone is not enough — checking the platform too keeps a
    stray binary on a Linux box from routing the install away from systemd."""
    return sys.platform == "darwin" and bool(shutil.which("launchctl"))


def plist_text():
    """The launchd LaunchAgent, as XML. Pure — testable without touching launchd.

    The macOS half of service_unit_text(); the two must stay behaviourally equal,
    so the reasoning below deliberately mirrors the [Service] block's.

    Built with plistlib rather than a format string: HOME is user-controlled and
    lands in five of these values, and a path holding & or < would emit XML that
    launchd rejects with nothing more useful than "Bootstrap failed: 5".
    """
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [_service_python(), "-m", "herd.daemon"],
        "EnvironmentVariables": {"PYTHONPATH": str(PKG_SRC), "HERD_DB": str(DB)},
        "RunAtLoad": True,
        # Plain true, NOT {"SuccessfulExit": False}: like Restart=always, and for
        # the same reason. A CLEAN exit still leaves nothing reaping silent deaths,
        # and the only symptom is sessions that never leave `herd ls` — so restart
        # on exit 0 too.
        "KeepAlive": True,
        # launchd's default throttle is 10s; 5 to match RestartSec=5. There is no
        # burst limit to disable (the systemd StartLimitIntervalSec=0 half) —
        # launchd throttles indefinitely and never latches into a failed state,
        # which is the behavior that setting was chosen to get.
        "ThrottleInterval": 5,
        # No ProcessType. It defaults to Standard; "Background" reads right for a
        # daemon but opts into CPU/IO throttling, and a throttled reaper shows up
        # as exactly the stale `herd ls` this service exists to prevent.
        "StandardOutPath": str(DAEMON_OUT),
        "StandardErrorPath": str(DAEMON_ERR),
    }).decode()


def _launchctl(*args):
    """launchctl, never raising and never hanging. Returns the CompletedProcess."""
    return subprocess.run(["launchctl", *args], check=False,
                          capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT)


def _gui_target():
    return f"gui/{os.getuid()}"


def install_launchd(dry=False):
    """Write + (re)load the LaunchAgent. Idempotent, like its systemd twin."""
    if dry:
        return f"would write {PLIST} and (re)load {LAUNCHD_LABEL}"
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    HERD_DIR.mkdir(parents=True, exist_ok=True)          # StandardOut/ErrPath's dir
    PLIST.write_text(plist_text())
    # bootout first so a rewritten plist is actually re-read: bootstrap on an
    # already-loaded label fails with EEXIST and would silently leave the OLD
    # definition running. Failure here just means "wasn't loaded" — expected on a
    # first install, so the result is ignored.
    _launchctl("bootout", f"{_gui_target()}/{LAUNCHD_LABEL}")
    r = _launchctl("bootstrap", _gui_target(), str(PLIST))
    if r.returncode != 0:
        # bootstrap/bootout are 10.11+. Fall back to the deprecated verbs rather
        # than fail the install on an older macOS.
        _launchctl("unload", str(PLIST))
        r = _launchctl("load", "-w", str(PLIST))
        if r.returncode != 0:
            return (f"LaunchAgent written to {PLIST} but load FAILED "
                    f"({(r.stderr or r.stdout).strip() or f'rc={r.returncode}'}) — "
                    f"load it yourself: launchctl bootstrap {_gui_target()} {PLIST}")
    # RunAtLoad has started it by now; report what launchd actually thinks.
    printed = _launchctl("print", f"{_gui_target()}/{LAUNCHD_LABEL}").stdout
    m = re.search(r"^\s*pid = (\d+)", printed, re.M)
    state = f"running (pid {m.group(1)})" if m else "loaded"
    return f"{LAUNCHD_LABEL} written + loaded ({state})"


def install_service(dry=False):
    """Write + enable + (re)start the daemon unit. Idempotent. systemd --user on
    Linux, a launchd LaunchAgent on macOS. Graceful no-op where neither exists
    (headless/containers) — herd still works, just run the daemon yourself."""
    if not _has_systemd_user():
        if _has_launchd():
            return install_launchd(dry)
        return ("daemon service SKIPPED — no systemctl --user or launchctl here. "
                "Run the daemon yourself:  PYTHONPATH=src python3 -m herd.daemon")
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


def uninstall_launchd():
    if not PLIST.exists():
        return "no LaunchAgent to remove"
    _launchctl("bootout", f"{_gui_target()}/{LAUNCHD_LABEL}")
    _launchctl("unload", str(PLIST))       # pre-10.11 fallback; a no-op after bootout
    PLIST.unlink(missing_ok=True)
    return f"removed {PLIST}"


def uninstall_service():
    if not _has_systemd_user():
        if _has_launchd():
            return uninstall_launchd()
        return "no herd.service to remove"
    if not SERVICE.exists():
        return "no herd.service to remove"
    subprocess.run(["systemctl", "--user", "disable", "--now", "herd.service"],
                   check=False, capture_output=True, timeout=SYSTEMCTL_TIMEOUT)
    SERVICE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False,
                   capture_output=True, timeout=SYSTEMCTL_TIMEOUT)
    return f"removed {SERVICE}"


# ── CLI on PATH + bash completion ──────────────────────────────────────────
def _relink(link, target, ts=None):
    """Idempotently point `link` at `target` (symlink). mkdir parents.

    A REAL FILE at the link path gets backed up first. This used to unlink
    whatever it found, so an unrelated ~/.local/bin/herd of your own was destroyed
    with no copy kept — and uninstall_cli() could not put it back, since it
    (correctly) only removes symlinks resolving to our own target. Every other
    path in this installer backs up before overwriting; this one didn't."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()                       # a symlink carries nothing to preserve
    elif link.exists():
        if link.is_dir():
            raise IsADirectoryError(f"{link} is a directory — refusing to replace it")
        backup(link, ts or _ts())
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


def statusline_plan(data, wrapper_exists, hooks_dir=None):
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
    sl = statusline_cmd(hooks_dir)
    cmd = _statusline_cmd(data)
    if not cmd or cmd == sl or "/.klawde/" in cmd:
        return "set"
    if cmd == str(WRAPPER):
        return "wrapper" if wrapper_exists else "set"     # dangling pointer -> ours
    return "foreign"


def _strip_managed(hooks):
    """Remove every herd-managed command in place, dropping blocks and then events
    that become empty. cdh / PreToolUse-HTTP / others are untouched.

    Shared by rewire_settings (which re-adds herd afterwards) and unwire_settings
    (which does not). It lives in one place because the two must agree on what herd
    owns — a strip that drifts from the re-add either doubles every hook or strands
    one behind."""
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


def rewire_settings(data, wrapper_exists=False, hooks_dir=None):
    """Return a NEW settings dict with herd wired and klawde unwired. Pure —
    caller decides whether to write it."""
    data = json.loads(json.dumps(data))          # deep copy
    hooks = data.setdefault("hooks", {})

    # 1. strip every herd-managed command (klawde + any prior herd).
    _strip_managed(hooks)

    # 2. add herd's authoritative entry for each event, in its OWN block at the
    #    front (so herd runs first; cdh keeps its block).
    for event, (script, is_async) in HERD_HOOKS.items():
        entry = {"type": "command", "command": hook_cmd(script, hooks_dir)}
        if is_async:
            entry["async"] = True
        hooks.setdefault(event, []).insert(0, {"hooks": [entry]})

    # 3. the statusline. Preserve any sibling keys (padding etc.) — only the
    #    command is ours to set.
    if statusline_plan(data, wrapper_exists, hooks_dir) == "set":
        sl = dict(data["statusLine"]) if isinstance(data.get("statusLine"), dict) else {}
        sl.update({"type": "command", "command": statusline_cmd(hooks_dir)})
        data["statusLine"] = sl

    return data


def unwire_settings(data, original=None):
    """Return a NEW settings dict with herd's edits REVERSED. Pure — the mirror of
    rewire_settings: same strip, no re-add.

    Reversing the edits, rather than restoring the pre-herd snapshot wholesale, is
    the point. Uninstall used to write a months-old copy over the live file, so
    every permission grant, MCP server and foreign hook added since the install was
    reverted with it — and no backup was taken, so it was gone. Anything herd does
    not own is carried through here untouched.

    `original` is the pre-herd settings dict when one survives. It is consulted for
    exactly ONE thing: what statusLine said before we set it. Nothing else is taken
    from it, because nothing else in it is more current than what is on disk now."""
    data = json.loads(json.dumps(data))          # deep copy
    hooks = data.setdefault("hooks", {})
    _strip_managed(hooks)
    if not hooks:
        del data["hooks"]            # we may have just created it; leave no residue

    # statusLine: only ours to touch. _is_managed is the same ownership test
    # install used to decide it could claim the key (klawde's counted as ours),
    # so the two stay symmetric. 'foreign' and wrapper-pointing values fail it and
    # are left exactly as found — install did not write them either.
    if _is_managed(_statusline_cmd(data)):
        prev = (original or {}).get("statusLine")
        if prev is not None:
            data["statusLine"] = prev            # verbatim, siblings and all
        else:
            data.pop("statusLine", None)         # the key was ours to add

    # preferredNotifChannel is deliberately LEFT. _offer_bell never overwrites an
    # existing value, so herd's opt-in and the user's own choice are the same two
    # bytes on disk — indistinguishable here. Deleting a real preference is the
    # worse error, so uninstall names the key instead and lets the user decide.
    return data


# ── statusline wrapper ─────────────────────────────────────────────────────
# The invocation token on a wrapper line: a quoted or bare path ending in
# statusline.sh. Substituting just this token keeps the REST of the line — the
# whole line used to be replaced by the path alone, silently dropping `exec`,
# `"$@"`, redirects and anything chained after it. A wrapper written as
# `exec "$HOME/.klawde/statusline.sh" "$@"` lost both exec and its arguments.
#
# The basename must be EXACTLY statusline.sh — `/statusline\.sh`, not a bare
# suffix. A composed wrapper chains OTHER tools' statuslines, and matching the
# suffix claimed them: the caveman plugin's `caveman-statusline.sh` looked like
# herd's own invocation and got rewritten to point at herd.
#
# The bare alternative also excludes `=`, so it cannot start before one. It was
# `\S*statusline\.sh`, which has no left boundary, so on
#     CAVEMAN_SL="$HOME/.../caveman-statusline.sh"
# it matched from column 0 — the assignment, the opening quote and the path are
# one unbroken run of non-space — and replaced the lot with a quoted herd path,
# leaving a stray `"` behind. That is a bash SYNTAX ERROR, so the wrapper emitted
# nothing at all and the statusline silently vanished from every session.
#
# `$( … )` MUST be part of the token, in both the quoted and bare forms. The
# first fix for the above still broke the commonest wrapper idiom there is:
#     exec "$(dirname "$0")/statusline.sh" "$@"
# The quoted alternative anchored on the INNER quotes — it matched `")/statusline.sh"`,
# the run from the closing quote of `"$0"` — and ate the `)` that closed the
# substitution. Same syntax error, same silent disappearance, different idiom.
# So a command substitution is matched as a unit (and may contain quotes), which
# also stops the quoted alternative from starting mid-string on a line like
#     echo "hi" ; exec "$SL/statusline.sh"
# where a naive non-greedy `"..."` would swallow `hi" ; exec "`.
_SUBST = r'\$\((?:[^()"\n]|"[^"\n]*")*\)'          # $( … ), quotes allowed inside
_SL_TOKEN = re.compile(
    rf'"(?:{_SUBST}|[^"\n])*/statusline\.sh"'      # "…/statusline.sh"
    rf"|'(?:{_SUBST}|[^'\n])*/statusline\.sh'"     # '…/statusline.sh'
    rf'|(?:{_SUBST}|[^\s"\'=])*/statusline\.sh'    # bare …/statusline.sh
    r'|(?<![\w./-])statusline\.sh'                 # bare, cwd-relative, no path
)


def wrapper_parses(text):
    """Does this wrapper parse as bash? Returns True when bash is unavailable —
    this is a safety net, not a gate we can afford to fail closed on."""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(text)
            path = fh.name
    except OSError:
        return True
    try:
        return subprocess.run(["bash", "-n", path], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def rewire_wrapper(text, hooks_dir=None):
    """Point the wrapper's statusline invocation at herd's, leaving the rest of
    each line intact. Idempotent.

    Comment lines are skipped: rewriting inside `#` changes nothing executable, but
    it still reported replaced=True, so the installer claimed to have wired a
    wrapper it had not.

    THE PARSE CHECK IS THE POINT. Twice now a regex that looked right has turned a
    working wrapper into a bash syntax error, and the blast radius is the same both
    times: a syntax error prints NOTHING, so the statusline silently disappears from
    every running session with nothing in any log to explain it. Rather than trust
    the third regex, refuse to hand back a wrapper that does not parse when the one
    we were given did. The caller then leaves the file alone and says so.
    """
    sl = statusline_cmd(hooks_dir)
    out = []
    replaced = False
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not _SL_TOKEN.search(line):
            out.append(line)
            continue
        out.append(_SL_TOKEN.sub(lambda _: f'"{sl}"', line))
        replaced = True
    new = "\n".join(out) + "\n"
    if replaced and not wrapper_parses(new) and wrapper_parses(text):
        return text, False           # we would have broken it — change nothing
    return new, replaced


def unwire_wrapper(text, original_text):
    """Point the wrapper's statusline invocation back at whatever it named before
    herd. Returns (text, changed). Pure — the mirror of rewire_wrapper.

    It needs the pre-herd text because the original token is the one thing the
    rewired file no longer records. Without a usable original we change NOTHING and
    say so: a wrapper pointed at a path we invented is worse than one still pointed
    at herd, which the user can see and edit."""
    if not original_text:
        return text, False
    m = _SL_TOKEN.search(original_text)
    if not m:
        return text, False
    token = m.group(0)                  # lambda, not a replacement string: the
    out = []                            # token is a path and may contain backslashes
    changed = False
    for line in text.splitlines():
        if _SL_TOKEN.search(line):
            out.append(_SL_TOKEN.sub(lambda _: token, line))
            changed = True
        else:
            out.append(line)
    return "\n".join(out) + "\n", changed


# ── self-test: run the WIRED hook against a temp DB ────────────────────────
def selftest(hooks_dir=None):
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

        hd = hooks_dir or INSTALLED_HOOKS
        not_exec = [p.name for p in sorted(hd.glob("*.sh"))
                    if not os.access(p, os.X_OK)]
        if not_exec:
            return False, {"not_executable": not_exec}

        try:
            subprocess.run([hook_cmd("session_start.sh", hd)],      # direct exec
                           input=f'{{"session_id":"{SID}","cwd":"/x","model":"m","source":"startup"}}',
                           capture_output=True, text=True, env=env,
                           timeout=SELFTEST_TIMEOUT)
            sl = subprocess.run([statusline_cmd(hd)],                # direct exec
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


def install(dry=False, dev=False):
    ts = _ts()
    print(f"herd install  (ts={ts}{', DEV' if dev else ''})\n")
    print("  " + bootstrap_db(dry))

    hooks_dir = sync_hooks(dev=dev, dry=dry)
    if dev:
        print(f"  hooks: WIRED TO THE CHECKOUT {hooks_dir}")
        print("     --dev: a git checkout/stash changes what running sessions execute.")
    else:
        print(f"  {'would copy' if dry else 'copied'} hooks + schema -> {hooks_dir}")

    # A first-time machine may have no settings.json at all — treat that as empty
    # rather than a traceback. rewire_settings() setdefaults "hooks", backup()
    # no-ops on a missing file, so the absent case needs no other special-casing.
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    plan = statusline_plan(settings, WRAPPER.exists(), hooks_dir)
    new_settings = rewire_settings(settings, wrapper_exists=WRAPPER.exists(),
                                   hooks_dir=hooks_dir)
    wrapper_text, wrap_ok = (rewire_wrapper(WRAPPER.read_text(), hooks_dir)
                             if WRAPPER.exists() else ("", False))
    sl_target = statusline_cmd(hooks_dir)
    sl_note = {
        "set":     f"statusLine -> {sl_target}",
        "wrapper": f"statusLine -> {WRAPPER} (rewired to herd below)",
        "foreign": (f"statusLine LEFT ALONE — it runs {_statusline_cmd(settings)!r}, "
                    "which herd does not own. Point it at\n      "
                    f"{sl_target} yourself, or herd records no cost/context/branch."),
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
    ok, row = selftest(hooks_dir)
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


def _revert_to_original(path, ts):
    """--restore-original: put the pre-herd snapshot back, wholesale. Backs up the
    live file first — that omission is what made this path destructive."""
    ref = _restore_source(path)
    if not ref:
        print(f"  NO PRE-HERD BACKUP for {path} — left as-is, edit it by hand")
        return 1
    backup(path, ts)
    _atomic_write(path, ref.read_text())
    print(f"  restored {path} from {ref.name}  (backup: *.herd-bak.{ts})")
    return 0


def _reference_settings(path):
    """The pre-herd settings dict, or None. Only statusLine is read from it."""
    ref = _restore_source(path)
    if not ref:
        return None
    try:
        return json.loads(ref.read_text())
    except (OSError, json.JSONDecodeError):
        return None            # unusable reference -> statusLine key is dropped


def _uninstall_settings(ts, restore_original):
    if not SETTINGS.exists():
        print(f"  no {SETTINGS} — nothing to unwire")
        return 0
    if restore_original:
        return _revert_to_original(SETTINGS, ts)
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Never traceback on the file whose truncation stops Claude from starting.
        print(f"  CANNOT READ {SETTINGS} ({e}) — left as-is.")
        print("    Remove herd's hook entries by hand, or re-run with --restore-original.")
        return 1
    new = unwire_settings(data, _reference_settings(SETTINGS))
    backup(SETTINGS, ts)
    _atomic_write(SETTINGS, json.dumps(new, indent=2) + "\n")
    print(f"  unwired {SETTINGS}  (backup: *.herd-bak.{ts})")
    if new.get("preferredNotifChannel"):
        print(f"    left preferredNotifChannel={new['preferredNotifChannel']!r} — herd cannot")
        print("    tell its own opt-in from your setting. Remove it by hand if unwanted.")
    return 0


def _uninstall_wrapper(ts, restore_original):
    if not WRAPPER.exists():
        print(f"  no {WRAPPER} — nothing to unwire")
        return 0
    if restore_original:
        return _revert_to_original(WRAPPER, ts)
    text = WRAPPER.read_text()
    if not _SL_TOKEN.search(text):
        print(f"  {WRAPPER} has no statusline invocation — nothing to unwire")
        return 0
    ref = _restore_source(WRAPPER)
    new_text, changed = unwire_wrapper(text, ref.read_text() if ref else None)
    if not changed:
        print(f"  {WRAPPER} LEFT AS-IS — no pre-herd statusline invocation to restore.")
        print("    It still calls herd's statusline; edit it by hand, or re-run with")
        print("    --restore-original.")
        return 1
    backup(WRAPPER, ts)
    _atomic_write(WRAPPER, new_text)
    print(f"  unwired {WRAPPER} statusline  (backup: *.herd-bak.{ts})")
    return 0


def uninstall(restore_original=False):
    """Reverse herd's edits to each file it wired, + remove service & CLI.

    Default is SURGICAL: strip what herd owns from the live file and leave the rest.
    --restore-original is the old wholesale revert to the pre-herd snapshot, kept as
    an escape hatch for a settings.json this cannot parse.

    Both paths back the file up before writing. Neither used to, and that was the
    bug: the wholesale revert wrote a months-old snapshot over the live file with no
    copy kept, so a month of permission grants, MCP servers and foreign hooks was
    unrecoverable."""
    print("  " + uninstall_service())
    print("  " + uninstall_cli())
    ts = _ts()
    rc = _uninstall_settings(ts, restore_original)
    return rc | _uninstall_wrapper(ts, restore_original)


# Every flag main() understands. An argv token outside this set is a TYPO, and the
# only safe reading of a typo on this command is "do nothing" — see main().
_FLAGS = {"--uninstall", "--restore-original", "--dry-run", "--dev", "--help", "-h"}

USAGE = """usage: python3 -m herd.install [--dev] [--dry-run]
       python3 -m herd.install --uninstall [--restore-original]

  (no flags)    wire herd from the installed copy in ~/.herd/hooks
  --dev         wire the CHECKOUT directly, for hook development
  --dry-run     show what would change, touch nothing
  --uninstall   reverse herd's edits, keeping everything else in settings.json
  --restore-original
                with --uninstall: revert wholesale to the pre-herd snapshot
                instead. Discards anything added since the install.
  --help, -h    this message"""


def main(argv=None):
    """Unknown argv is REFUSED, not ignored.

    This used to be `install(dry="--dry-run" in argv, ...)` — a membership test per
    flag and no validation — so every unrecognized token fell through to a full
    install. `--help` installed. `--dry-runn` installed, having been asked to touch
    nothing. That is the worst possible reading of a typo on a command that rewrites
    settings.json, rewires the statusline and restarts a systemd unit.

    A flag this command does not understand means the caller wanted something we are
    not doing, so the only safe move is to do NOTHING and say so.
    """
    argv = argv if argv is not None else sys.argv[1:]
    unknown = [a for a in argv if a not in _FLAGS]
    if unknown:
        print(f"herd install: unknown option {', '.join(repr(a) for a in unknown)}")
        print("  Nothing was changed. This command rewrites settings.json and")
        print("  restarts a service, so an option it cannot read means it stops.")
        print()
        print(USAGE)
        return 2
    # Validating tokens INDIVIDUALLY was not enough. `--dry-run --uninstall` passed
    # — both are real flags — and then `--uninstall` won the dispatch below, so it
    # unwired settings.json, deleted the service and removed the symlinks, having
    # been explicitly told to touch nothing. uninstall() takes no `dry`, so there is
    # nothing to honour; the only safe answer is to refuse the combination.
    if "--uninstall" in argv:
        conflicts = [a for a in ("--dry-run", "--dev") if a in argv]
        if conflicts:
            print(f"herd install: --uninstall cannot be combined with "
                  f"{', '.join(conflicts)}")
            print("  Nothing was changed. --uninstall has no dry-run and ignores")
            print("  --dev, so accepting these would have done something other than")
            print("  what you asked for.")
            return 2
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--uninstall" in argv:
        # zero-arg on the common path: --restore-original only ever ADDS a keyword,
        # so the plain call stays the plain call.
        return uninstall(restore_original=True) if "--restore-original" in argv \
            else uninstall()
    if "--restore-original" in argv:
        print("herd install: --restore-original means nothing without --uninstall.")
        print("  Nothing was changed.")
        print()
        print(USAGE)
        return 2
    return install(dry="--dry-run" in argv, dev="--dev" in argv)


if __name__ == "__main__":
    sys.exit(main() or 0)
