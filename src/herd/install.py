"""herd installer — cut over from klawde (capture only).

    python3 -m herd.install            # wire herd, unwire klawde (backs up first)
    python3 -m herd.install --uninstall  # restore the pre-herd state
    python3 -m herd.install --dry-run    # show what would change, touch nothing
    python3 -m herd.install --dev        # wire the CHECKOUT, not the installed copy

Hooks are COPIED to ~/.herd/hooks (with the SQL they read) and settings.json is
wired there, so a git checkout in the source tree cannot change what running
Claude sessions execute. `--dev` wires the checkout directly.

Idempotent. Every edited file is backed up as <file>.herd-bak.<ts> before the
first change, and once as <file>.herd-bak.original — the pre-herd copy uninstall
restores. Nothing is rewired or copied until the self-test passes; a FAIL aborts and
exits nonzero. (The herd dir, the config and the DB are created before the gate —
bootstrap_db runs first, so a FAIL still leaves those behind.) Leaves klawde's repo and ~/.klawde/sessions.db in place — only unwires
it from settings.json.
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

from herd import config as herd_config
from herd import settings as _settings
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


def config_path():
    """Settings the daemon and the hooks BOTH read — see config.py. A FUNCTION, not
    a module constant, so it follows a patched HERD_DIR."""
    return HERD_DIR / "config"

# Where the hooks that actually RUN live. Wiring settings.json at the checkout
# instead would make a checkout/stash/rebase silently change running sessions, and
# moving the clone would break all five hooks. `--dev` opts into that.
INSTALLED_HOOKS = HERD_DIR / "hooks"
INSTALLED_SCHEMA = HERD_DIR / "schema"
SERVICE = HOME / ".config" / "systemd" / "user" / "herd.service"
# macOS: same daemon as a LaunchAgent. The label doubles as the launchctl handle.
LAUNCHD_LABEL = "com.codingzen.herd"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
# launchd keeps no journal — the daemon's stderr is the only record of a crash loop.
DAEMON_OUT = HERD_DIR / "daemon.out.log"
DAEMON_ERR = HERD_DIR / "daemon.err.log"
CLI_SRC = REPO / "bin" / "herd"
CLI_LINK = HOME / ".local" / "bin" / "herd"
COMPLETION_SRC = REPO / "completions" / "herd.bash"
COMPLETION_LINK = HOME / ".local" / "share" / "bash-completion" / "completions" / "herd"

# Defined in settings.py, which doctor reads too. Re-exported: tests and the rest
# of this module address them here.
HERD_HOOKS = _settings.HERD_HOOKS
_OUR_SCRIPTS = _settings.OUR_SCRIPTS

STATUSLINE = str(INSTALLED_HOOKS / "statusline.sh")

# systemctl can block on a busy/degraded manager; the installer must not hang.
SYSTEMCTL_TIMEOUT = 15
# launchctl blocks the same way when launchd is wedged.
LAUNCHCTL_TIMEOUT = 15
# A wired hook that hangs would hang the self-test that exists to vet it.
SELFTEST_TIMEOUT = 20

_OUR_SCRIPTS = {s for s, _ in HERD_HOOKS.values()} | {"statusline.sh", "common.sh"}


def hook_cmd(script, hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / script)


def statusline_cmd(hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / "statusline.sh")


def _is_managed(cmd):
    """Delegates to settings.is_managed. The roots come from THIS module, so a test
    that redirects INSTALLED_HOOKS still works."""
    return _settings.is_managed(cmd, (HOOKS_DIR, INSTALLED_HOOKS))


def _ts():
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
    """Does this file already point at herd? BOTH roots, or a copy-mode install
    reads as pristine and overwrites the pre-herd snapshot with a herd-wired one."""
    if not text:
        return False
    return str(HOOKS_DIR) in text or str(INSTALLED_HOOKS) in text


def backup_original(path, text):
    """Snapshot the PRE-HERD file, exactly once, under a fixed name.

    The timestamped backups are useless for uninstall: the second install backs up
    the already-wired file. This copy is never overwritten."""
    b = path.with_name(path.name + ORIGINAL_SUFFIX)
    if b.exists() or not path.exists() or _is_wired(path, text):
        return None
    shutil.copy2(path, b)
    return b


def _restore_source(path):
    """The backup uninstall should restore: the pristine original if we have one,
    else the OLDEST timestamped backup (on a pre-ORIGINAL_SUFFIX install, the last
    copy that predates herd). None when nothing usable exists.

    A timestamped candidate must still be CHECKED, the same way backup_original
    checks before creating the pristine copy. Without that, uninstall reinstated
    herd: with no ~/.claude/settings.json on the machine, install #1 has nothing to
    snapshot, so no .original is ever written; install #2 then timestamps the
    ALREADY-WIRED file, and the oldest timestamped backup is a herd-wired one.
    Uninstall read its statusLine back out and put herd's own statusline in, or with
    --restore-original wrote the whole wired file back — and printed success either
    way. Returning None instead is correct and loud: every caller degrades to a
    surgical unwire or says the file must be edited by hand."""
    orig = path.with_name(path.name + ORIGINAL_SUFFIX)
    if orig.exists():
        return orig
    baks = sorted(path.parent.glob(path.name + ".herd-bak.*"))
    for b in baks:
        if b.name == orig.name:
            continue
        try:
            text = b.read_text()
        except OSError:
            continue           # unreadable candidate: try the next-oldest
        if not _is_wired(b, text):
            return b
    return None


def _atomic_write(path, text):
    """Write via tmp + rename in the same directory. write_text() truncates in
    place, and a crash mid-write to settings.json leaves JSON that stops Claude Code
    from starting.

    The REPLACEMENT INHERITS THE MODE OF WHAT IT REPLACES (the copymode below):
    os.replace does not carry it across, so without that a 0755 wrapper becomes
    0664 — Claude execs a file it cannot execute and the statusline silently
    vanishes — and a 0600 settings.json widens to 0664."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".herd-tmp.{os.getpid()}")
    try:
        tmp.write_text(text)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ── hook installation (copy, or point at the checkout with --dev) ──────────
def _copy_hook_tree(hooks_dst, schema_dst):
    """Copy hooks + SQL into a {hooks,schema} pair and return the hooks dir.

    The two must always be SIBLINGS: common.sh resolves HERD_WRITES as
    <hooks>/../schema/writes.sql, so hooks installed without their schema fail every
    write with "no such statement" while still exiting 0."""
    hooks_dst.mkdir(parents=True, exist_ok=True)
    schema_dst.mkdir(parents=True, exist_ok=True)
    for src in sorted(HOOKS_DIR.glob("*.sh")):
        dst = hooks_dst / src.name
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | 0o111)   # +x, or the hook is a silent no-op
    for src in sorted(SCHEMA_DIR.glob("*.sql")):
        shutil.copy2(src, schema_dst / src.name)
    return hooks_dst


def stage_hooks(root):
    """Copy the hooks into a throwaway <root>/{hooks,schema} for the self-test.

    The gate is only a gate if it runs on a copy nothing executes yet."""
    root = pathlib.Path(root)
    return _copy_hook_tree(root / "hooks", root / "schema")


def sync_hooks(dev=False, dry=False):
    """Put the hooks where they will be RUN from, and return that directory."""
    if dev:
        return HOOKS_DIR
    if dry:
        return INSTALLED_HOOKS
    return _copy_hook_tree(INSTALLED_HOOKS, INSTALLED_SCHEMA)


def hooks_are_current(hooks_dir=None):
    """Do the installed copies match the checkout? False means the tree moved on
    without a re-install — what `herd doctor` reports."""
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
        return (f"would create {DB} and apply core.sql + herd.sql "
                f"(+ templates dir, + {config_path()} if absent)")
    HERD_DIR.mkdir(parents=True, exist_ok=True)
    (HERD_DIR / "templates").mkdir(exist_ok=True)   # spawn presets (herd spawn -t)
    # NEVER overwritten: the user edits this. Written commented-out, so a fresh
    # file changes nothing.
    cfg = config_path()
    if not cfg.exists():
        _atomic_write(cfg, herd_config.DEFAULT_TEXT)
    # The ONE place allowed to bring the database into being — see db.connect.
    conn = connect(str(DB), create=True)
    apply_schema(conn)          # idempotent: CREATE TABLE IF NOT EXISTS
    conn.close()
    return f"bootstrapped {DB} + {HERD_DIR / 'templates'}/ + {cfg}"


# ── daemon service (reaper + attention) ────────────────────────────────────
# The daemon runs from the source tree with PYTHONPATH — no pip install needed.
def _service_python():
    """Prefer system python3 over whatever ran the installer — often a pyenv shim
    needing PATH a systemd unit won't have. herd is stdlib-only, any 3.9+."""
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(cand, os.X_OK):
            return cand
    return sys.executable


def _has_systemd_user():
    return bool(shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR"))


def _systemctl(*args):
    """systemctl --user, never raising and never hanging. Returns a CompletedProcess;
    failures show as a nonzero returncode, so callers need no new branch.

    `check=False` suppresses only CalledProcessError — `timeout=` still RAISES
    TimeoutExpired, the case the bound exists for. install() runs install_service()
    *after* rewriting settings.json, so an escaping exception leaves the config
    changed and no daemon."""
    try:
        return subprocess.run(["systemctl", "--user", *args], check=False,
                              capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"systemctl {args[0]} timed out after {SYSTEMCTL_TIMEOUT}s")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", f"systemctl: {e}")


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
        # PYTHONPATH ONLY — no herd setting belongs here. The hooks cannot read a
        # unit file, so anything named here is seen by half of herd and WINS over
        # ~/.herd/config. Set HERD_DB there instead; daemon.DEFAULT_DB falls back to
        # ~/.herd/herd.db and systemd --user does provide HOME.
        f"ExecStart={_service_python()} -m herd.daemon\n"
        # always, not on-failure: a clean exit still means nothing is reaping silent
        # deaths. With StartLimitIntervalSec=0 above, a persistent fault keeps
        # retrying instead of latching into `failed`.
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _has_launchd():
    """macOS. Check the platform too, or a stray `launchctl` on a Linux box routes
    the install away from systemd."""
    return sys.platform == "darwin" and bool(shutil.which("launchctl"))


def plist_text():
    """The launchd LaunchAgent, as XML. Pure — testable without touching launchd.
    The macOS half of service_unit_text(); the two must stay behaviourally equal.

    plistlib rather than a format string: HOME is user-controlled and lands in five
    of these values, and a path holding & or < emits XML launchd rejects with only
    "Bootstrap failed: 5".
    """
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [_service_python(), "-m", "herd.daemon"],
        "EnvironmentVariables": {"PYTHONPATH": str(PKG_SRC), "HERD_DB": str(DB)},
        "RunAtLoad": True,
        # Plain true, NOT {"SuccessfulExit": False} — the Restart=always half:
        # restart on exit 0 too.
        "KeepAlive": True,
        # launchd's default throttle is 10s; 5 to match RestartSec=5. No burst limit
        # to disable — launchd throttles indefinitely and never latches into failed.
        "ThrottleInterval": 5,
        # No ProcessType (defaults to Standard). "Background" opts into CPU/IO
        # throttling, and a throttled reaper is the stale `herd ls` this prevents.
        "StandardOutPath": str(DAEMON_OUT),
        "StandardErrorPath": str(DAEMON_ERR),
    }).decode()


def _launchctl(*args):
    """launchctl, never raising and never hanging. The macOS twin of _systemctl —
    same bound, same reason. Returns a CompletedProcess; failures show as a nonzero
    returncode."""
    try:
        return subprocess.run(["launchctl", *args], check=False,
                              capture_output=True, text=True,
                              timeout=LAUNCHCTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"launchctl {args[0]} timed out after {LAUNCHCTL_TIMEOUT}s")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", f"launchctl: {e}")


def _gui_target():
    return f"gui/{os.getuid()}"


def install_launchd(dry=False):
    """Write + (re)load the LaunchAgent. Idempotent, like its systemd twin."""
    if dry:
        return f"would write {PLIST} and (re)load {LAUNCHD_LABEL}"
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    HERD_DIR.mkdir(parents=True, exist_ok=True)          # StandardOut/ErrPath's dir
    PLIST.write_text(plist_text())
    # bootout first so a rewritten plist is re-read: bootstrap on an already-loaded
    # label fails with EEXIST and silently leaves the OLD definition running.
    # Failure here just means "wasn't loaded", expected on a first install.
    _launchctl("bootout", f"{_gui_target()}/{LAUNCHD_LABEL}")
    r = _launchctl("bootstrap", _gui_target(), str(PLIST))
    if r.returncode != 0:
        # bootstrap/bootout are 10.11+; fall back to the deprecated verbs.
        _launchctl("unload", str(PLIST))
        r = _launchctl("load", "-w", str(PLIST))
        if r.returncode != 0:
            return (f"LaunchAgent written to {PLIST} but load FAILED "
                    f"({(r.stderr or r.stdout).strip() or f'rc={r.returncode}'}) — "
                    f"load it yourself: launchctl bootstrap {_gui_target()} {PLIST}")
    # RunAtLoad has started it by now; report what launchd thinks.
    printed = _launchctl("print", f"{_gui_target()}/{LAUNCHD_LABEL}").stdout
    m = re.search(r"^\s*pid = (\d+)", printed, re.M)
    state = f"running (pid {m.group(1)})" if m else "loaded"
    return f"{LAUNCHD_LABEL} written + loaded ({state})"


def install_service(dry=False):
    """Write + enable + (re)start the daemon unit. Idempotent. systemd --user on
    Linux, a LaunchAgent on macOS, a no-op where neither exists."""
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
        _systemctl(*args)
    active = (_systemctl("is-active", "herd.service").stdout or "").strip()
    return f"herd.service written + enabled ({active or 'unknown'})"


def uninstall_launchd():
    if not PLIST.exists():
        return "no LaunchAgent to remove"
    _launchctl("bootout", f"{_gui_target()}/{LAUNCHD_LABEL}")
    _launchctl("unload", str(PLIST))       # pre-10.11 fallback; no-op after bootout
    PLIST.unlink(missing_ok=True)
    return f"removed {PLIST}"


def uninstall_service():
    if not _has_systemd_user():
        if _has_launchd():
            return uninstall_launchd()
        return "no herd.service to remove"
    if not SERVICE.exists():
        return "no herd.service to remove"
    _systemctl("disable", "--now", "herd.service")
    SERVICE.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return f"removed {SERVICE}"


# ── CLI on PATH + bash completion ──────────────────────────────────────────
def _relink(link, target, ts=None):
    """Idempotently point `link` at `target` (symlink). mkdir parents.

    A REAL FILE at the link path is backed up first — uninstall_cli() only removes
    symlinks resolving to our own target, so it cannot put one back."""
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
# herd does NOT own this — a Claude-level, terminal-specific preference. Offered
# only: opt-in, a tip on a non-tty, and never overriding an existing choice.
def _bell_decision(current, answer):
    """Pure: the channel to set, or None to leave unchanged. Respects any existing
    preferredNotifChannel."""
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


# ── kitty.conf: remote control, the one thing herd needs and cannot see ────
# The only file herd writes that it does not own. Hence: opt-in, marker-delimited,
# backed up, removed again by --uninstall. Offered at all because without these two
# options herd records an empty placement for every session and spawn/jump cannot
# work — and nothing about that failure points here.
def _kitty_decision(state, has_block, answer):
    """Pure: should we write the block? Only when we KNOW it is missing (in kitty,
    no socket) and herd has not already added it. `not-kitty` never writes — from
    outside kitty we cannot see the config, and appending on a guess edits a working
    file. `ready` never writes: the options are already on."""
    from herd.kitty import config
    if state != config.OFF or has_block:
        return False
    return answer.strip().lower() in ("y", "yes")


def _kitty_tip(lead="kitty remote control is OFF"):
    """The advice, with a caller-supplied opening clause (the decline path already
    says "skipped")."""
    from herd.kitty import config
    return (f"{lead} — herd will record no window for any session and\n"
            f"    spawn/jump cannot work. Add to {config.KITTY_CONF}:\n"
            "      allow_remote_control yes\n"
            "      listen_on unix:/tmp/kitty-{kitty_pid}\n"
            f"    then {config.RESTART}.  `herd doctor` verifies it.")


def _offer_kitty(dry=False, environ=None, answer=None):
    """Interactively offer to enable kitty remote control. Returns a status line."""
    from herd.kitty import config
    environ = environ if environ is not None else os.environ
    state = config.state(environ)
    if state == config.READY:
        return "kitty remote control already enabled — left as-is"
    text = config.KITTY_CONF.read_text() if config.KITTY_CONF.exists() else ""
    if config.has_block(text):
        return f"kitty remote control block already in {config.KITTY_CONF.name} — " \
               f"{config.RESTART}"
    if state == config.NOT_KITTY:
        # Not a prompt: outside kitty a missing config and a fine one look alike.
        return ("kitty not detected — if you use kitty, see README (kitty setup); "
                "`herd doctor` in a kitty window checks it")
    if dry:
        return f"would offer to add remote control to {config.KITTY_CONF}"
    if answer is None:
        if not sys.stdin.isatty():
            return _kitty_tip()
        answer = input(
            f"  Enable kitty remote control? herd needs it to record which window a\n"
            f"  session is in and to jump there. Appends 2 lines to {config.KITTY_CONF}\n"
            "  (backed up, and removed again by --uninstall) [y/N]: ")
    if not _kitty_decision(state, False, answer):
        return _kitty_tip("kitty remote control skipped")
    backup(config.KITTY_CONF, _ts())
    _atomic_write(config.KITTY_CONF, config.add_block(text))
    return f"kitty remote control enabled in {config.KITTY_CONF} — {config.RESTART}"


def _uninstall_kitty(ts):
    """Remove herd's block. A kitty.conf herd never touched is left byte-identical."""
    from herd.kitty import config
    if not config.KITTY_CONF.exists():
        return "no kitty.conf to clean"
    text = config.KITTY_CONF.read_text()
    if not config.has_block(text):
        return "kitty.conf untouched by herd — left alone"
    backup(config.KITTY_CONF, ts)
    _atomic_write(config.KITTY_CONF, config.strip_block(text))
    return f"removed herd's block from {config.KITTY_CONF} — {config.RESTART}"


# ── settings.json surgery ──────────────────────────────────────────────────
def _statusline_cmd(data):
    """Delegates to settings.statusline_command, which survives a statusLine that is
    a string or a list."""
    return _settings.statusline_command(data)


def statusline_plan(data, wrapper_exists, hooks_dir=None):
    """What settings.statusLine needs, as one of:

    'wrapper' — an existing custom-status-line.sh already sits in front of it, and
                rewire_wrapper points that at herd. Leave the key alone.
    'set'     — absent, klawde's, or already ours: wire herd's statusline directly.
    'foreign' — someone else's statusline. Never clobber it; say so instead.

    Pure, so install() can report it without re-deriving the decision. 'set' must
    stay: without it, wiring happens only through the wrapper, so a machine with no
    wrapper gets no statusline — and statusline.sh writes every metric column."""
    sl = statusline_cmd(hooks_dir)
    cmd = _statusline_cmd(data)
    if not cmd or cmd == sl or "/.klawde/" in cmd:
        return "set"
    if cmd == str(WRAPPER):
        return "wrapper" if wrapper_exists else "set"     # dangling pointer -> ours
    return "foreign"


def _strip_managed(hooks):
    """Delegates to settings.strip_managed, which is tolerant of the shapes a
    hand-edited settings.json can hold."""
    _settings.strip_managed(hooks, (HOOKS_DIR, INSTALLED_HOOKS))


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
    rewire_settings: same strip, no re-add. Reversing the edits rather than
    restoring the pre-herd snapshot is the point: every permission grant, MCP server
    and foreign hook added since the install is carried through untouched.

    `original` is the pre-herd settings dict when one survives, consulted for
    exactly ONE thing: what statusLine said before we set it. Nothing else in it is
    more current than what is on disk now."""
    data = json.loads(json.dumps(data))          # deep copy
    hooks = data.setdefault("hooks", {})
    _strip_managed(hooks)
    if not hooks:
        del data["hooks"]            # we may have just created it; leave no residue

    # statusLine: only ours to touch. _is_managed is the SAME ownership test install
    # used to claim the key, so the two stay symmetric — 'foreign' and
    # wrapper-pointing values fail it and are left as found, since install did not
    # write them either.
    if _is_managed(_statusline_cmd(data)):
        prev = (original or {}).get("statusLine")
        if prev is not None:
            data["statusLine"] = prev            # verbatim, siblings and all
        else:
            data.pop("statusLine", None)         # the key was ours to add

    # preferredNotifChannel is deliberately LEFT: _offer_bell never overwrites an
    # existing value, so herd's opt-in and the user's own choice are indistinguishable
    # here. Uninstall names the key instead and lets the user decide.
    return data


# ── statusline wrapper ─────────────────────────────────────────────────────
# The invocation token on a wrapper line: a quoted or bare path ending in
# statusline.sh. Only the TOKEN is substituted, so the rest of the line survives —
# replacing the whole line drops `exec`, `"$@"`, redirects and anything chained.
#
# Three constraints. Breaking any of them yields a bash syntax error, and a wrapper
# that cannot parse prints NOTHING — the statusline vanishes with no log anywhere:
#
#  1. The basename must be EXACTLY statusline.sh (`/statusline\.sh`, never a bare
#     suffix), or a composed wrapper's other tools get claimed — e.g.
#     `caveman-statusline.sh`.
#  2. The bare alternative excludes `=`, giving it a left boundary. Without one,
#     `SL="$HOME/.../statusline.sh"` matches from column 0 (assignment, quote and
#     path are one unbroken non-space run) and leaves a stray `"`.
#  3. `$( … )` must match as a UNIT in both forms, or on
#     `exec "$(dirname "$0")/statusline.sh" "$@"` the quoted alternative anchors on
#     the INNER quotes and eats the `)`. It also stops the quoted form starting
#     mid-string on `echo "hi" ; exec "$SL/statusline.sh"`.
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

    Comment lines are skipped: rewriting inside `#` changes nothing executable but
    would still report replaced=True.

    THE PARSE CHECK IS THE POINT — never trust the regex. If our rewrite fails to
    parse and the input did, hand back the input unchanged; the caller leaves the
    file alone and says so.
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

    Needs the pre-herd text: the original token is the one thing the rewired file no
    longer records. Without a usable original, change NOTHING — a wrapper pointed at
    a path we invented is worse than one still pointed at herd."""
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
    """Prove the wired hooks actually write, using a throwaway DB.

    Invokes each script the way PRODUCTION does — directly, NOT via `bash <path>`.
    settings.json and the wrapper exec these paths, so a missing +x is a silent
    no-op that running them through `bash` here would mask.
    """
    # session_id must satisfy valid_sid() — alphanumerics + hyphens only.
    SID = "herd-selftest-0000-4000-8000-000000000001"
    with tempfile.TemporaryDirectory(prefix="herd-selftest-") as tmp:
        env = dict(os.environ, HERD_DB=f"{tmp}/t.db", HERD_RUNTIME=tmp,
                   HERD_ERRLOG=f"{tmp}/err.log")
        c = connect(f"{tmp}/t.db", create=True); apply_schema(c); c.close()

        hd = hooks_dir or INSTALLED_HOOKS
        not_exec = [p.name for p in sorted(hd.glob("*.sh"))
                    if not os.access(p, os.X_OK)]
        if not_exec:
            return False, {"not_executable": not_exec}

        # PARSE EVERY SCRIPT — the exec path below only runs session_start.sh and
        # statusline.sh, so without this a stop.sh with a syntax error passes the
        # gate. A hook that cannot parse silently does nothing.
        unparseable = {}
        for p in sorted(hd.glob("*.sh")):
            r = subprocess.run(["bash", "-n", str(p)], capture_output=True,
                               text=True, timeout=SELFTEST_TIMEOUT)
            if r.returncode != 0:
                unparseable[p.name] = r.stderr.strip().splitlines()[-1:] or ["?"]
        if unparseable:
            return False, {"syntax_error": unparseable}

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
            # A hook that hangs is a FAIL, not a hung installer.
            return False, {"timed_out": e.cmd[0] if e.cmd else "?"}
        c = connect(f"{tmp}/t.db", create=True)
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

    # NOTHING IS COPIED YET on the real path. hooks_dir names where the hooks WILL
    # run from so the settings/wrapper plan can be built against it; the copy is
    # deferred until the self-test below passes.
    hooks_dir = sync_hooks(dev=dev, dry=True) if not dev else sync_hooks(dev=True)
    if dev:
        print(f"  hooks: WIRED TO THE CHECKOUT {hooks_dir}")
        print("     --dev: a git checkout/stash changes what running sessions execute.")
    elif dry:
        print(f"  would copy hooks + schema -> {hooks_dir}")
    else:
        print(f"  will copy hooks + schema -> {hooks_dir} (after the self-test passes)")

    # No settings.json is just empty: rewire_settings() setdefaults "hooks" and
    # backup() no-ops on a missing file. NOT a bare json.loads — a truncated or
    # mode-000 file must not traceback out of the command you run BECAUSE your
    # config is broken.
    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except (OSError, ValueError) as e:       # ValueError covers JSONDecodeError
            print(f"herd install: cannot read {SETTINGS} — {e}")
            print("  fix the file (or move it aside) and re-run; nothing was changed.")
            return 2
    if not isinstance(settings, dict):
        # A list or a string parses fine and is not a settings file; wiring into it
        # produces something Claude cannot read.
        print(f"herd install: {SETTINGS} is not a JSON object — nothing was changed.")
        return 2
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        # rewire_settings does hooks.setdefault(event, ...) — AttributeError on a
        # list. Refuse rather than coerce: replacing it with {} deletes whatever the
        # user meant.
        print(f"herd install: {SETTINGS} has a `hooks` key that is not an object "
              f"({type(settings['hooks']).__name__}) — nothing was changed.")
        return 2
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
        print("  " + _offer_kitty(dry=True))
        print("\n  resulting hooks:")
        for e, blocks in new_settings["hooks"].items():
            cmds = [h.get("command") or f"<{h.get('type','?')}:{h.get('url','')}>"
                    for b in blocks for h in b["hooks"]]
            print(f"    {e}: {cmds}")
        return

    # GATE THE WRITE — on a STAGED copy. settings.json already points at
    # ~/.herd/hooks/, so copying there and THEN self-testing puts broken hooks live
    # in every running session before the "ABORTED" line prints; the wiring never
    # has to change for the damage to land. So: copy into a temp {hooks,schema}
    # pair, exec THAT, promote only on PASS. --dev runs the checkout by definition,
    # so there is nothing to stage.
    if dev:
        ok, row = selftest(hooks_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="herd-staging-") as staging:
            ok, row = selftest(stage_hooks(staging))
    print(f"\n  self-test (staged hooks -> temp DB): {'PASS' if ok else 'FAIL'}  {row}")
    if not ok:
        print("\n  ABORTED — nothing was rewired and nothing was copied. The hooks do")
        print("  not work as installed; wiring them would have broken every Claude")
        print("  session silently. The hooks already on disk are untouched.")
        return 1

    if not dev:
        sync_hooks()                    # PROMOTE: the staged copy passed
        print(f"  copied hooks + schema -> {hooks_dir}")

    had_bell = "preferredNotifChannel" in new_settings
    bell_note = _offer_bell(new_settings)   # interactive; may set the key
    # _offer_bell blocks on input() and Claude Code writes settings.json (permission
    # grants) while we wait, so re-read and merge ONLY the keys herd owns onto the
    # fresh copy — NOT `fresh.update(new_settings)`, which replaces whole top-level
    # values and puts our stale `permissions` back over the fresh one. statusLine is
    # ours only when the plan said 'set'.
    owned = ["hooks"]
    if plan == "set":
        owned.append("statusLine")
    if "preferredNotifChannel" in new_settings and not had_bell:
        owned.append("preferredNotifChannel")
    if SETTINGS.exists():
        try:
            fresh = json.loads(SETTINGS.read_text())
            for k in owned:
                if k in new_settings:
                    fresh[k] = new_settings[k]
            new_settings = fresh
        except (OSError, json.JSONDecodeError):
            pass                                # unreadable — go with what we built
    backup_original(SETTINGS, SETTINGS.read_text() if SETTINGS.exists() else "")
    backup(SETTINGS, ts)
    _atomic_write(SETTINGS, json.dumps(new_settings, indent=2) + "\n")
    print(f"  rewired {SETTINGS} (backup: *.herd-bak.{ts})")
    print(f"  {sl_note}")
    if WRAPPER.exists():
        # wrap_ok, not WRAPPER.exists(): the wrapper may name no statusline at all,
        # or rewire_wrapper's parse check may have REFUSED the rewrite. In both
        # wrapper_text IS the input — nothing to write, and no backup to take.
        if wrap_ok:
            backup_original(WRAPPER, WRAPPER.read_text())
            backup(WRAPPER, ts)
            _atomic_write(WRAPPER, wrapper_text)
            print(f"  rewired {WRAPPER} statusline -> herd")
        else:
            print(f"  {WRAPPER} LEFT AS-IS — no statusline invocation herd could rewire")
            print("    safely (the rewrite would not have parsed). Point it at")
            print(f"    {sl_target} by hand if you want herd's statusline.")

    print("  " + install_service())
    print("  " + install_cli())
    print("  " + bell_note)
    print("  " + _offer_kitty())
    print("\n  use it (new shell picks up `herd` + completion):")
    print("    herd ls        # live sessions, attention-first, by name")
    print("    herd jump      # fuzzy-pick (fzf) a session and focus its window")
    print("\n  klawde is unwired but NOT deleted — ~/.klawde/sessions.db (history) is kept.")
    print(f"  restore:  python3 -m herd.install --uninstall")
    return 0


def _revert_to_original(path, ts):
    """--restore-original: put the pre-herd snapshot back, wholesale. Backs up the
    live file first — without that this path is destructive."""
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


def _uninstall_hook_tree():
    """Remove the hook tree _copy_hook_tree installed, and say so.

    Install created ~/.herd/{hooks,schema} and uninstall left them there, unmentioned
    — asymmetric, and it used to compound the _restore_source bug: the orphaned
    statusline.sh stayed on disk and executable while settings.json still pointed at
    it. Nothing points at it now, but "uninstalled" should not leave herd's
    executables behind.

    SURGICAL, like the rest of uninstall: only the extensions we wrote, then rmdir
    only if that emptied the directory. A file someone else put in there is theirs,
    and is reason to keep the directory rather than to delete it anyway.

    REFUSES the source checkout. `--dev` wires ~/.claude/settings.json straight at
    the repo, so INSTALLED_HOOKS and HOOKS_DIR can be the same directory — an
    unguarded rmtree there deletes the working tree, not an install."""
    out = []
    for d, pattern, src in ((INSTALLED_HOOKS, "*.sh", HOOKS_DIR),
                            (INSTALLED_SCHEMA, "*.sql", SCHEMA_DIR)):
        if not d.exists():
            continue
        try:
            same = d.resolve() == src.resolve()
        except OSError:                 # unresolvable: assume the dangerous case
            same = True
        if same:
            out.append(f"LEFT {d} — that is the checkout itself (--dev install)")
            continue
        for f in sorted(d.glob(pattern)):
            try:
                f.unlink()
            except OSError as e:
                out.append(f"could not remove {f}: {e}")
        try:
            d.rmdir()
            out.append(f"removed {d}")
        except OSError:
            out.append(f"removed herd's files from {d} (kept — not empty)")
    return out or [f"no installed hook tree under {HERD_DIR}"]


def uninstall(restore_original=False):
    """Reverse herd's edits to each file it wired, + remove service & CLI.

    Default is SURGICAL: strip what herd owns from the live file, leave the rest.
    --restore-original is the wholesale revert to the pre-herd snapshot, an escape
    hatch for a settings.json this cannot parse. Both paths back the file up first —
    without that the wholesale revert drops a months-old snapshot over the live
    file with no copy kept."""
    print("  " + uninstall_service())
    print("  " + uninstall_cli())
    ts = _ts()
    print("  " + _uninstall_kitty(ts))
    rc = _uninstall_settings(ts, restore_original)
    rc |= _uninstall_wrapper(ts, restore_original)
    # After the wiring, not before: if unwiring fails the files are still referenced,
    # and removing them first would leave settings.json pointing at nothing.
    for line in _uninstall_hook_tree():
        print("  " + line)
    return rc


# Every flag main() understands. Anything outside this set is a TYPO — see main().
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
    """Unknown argv is REFUSED, not ignored. Membership tests alone
    (`install(dry="--dry-run" in argv)`) let every unrecognized token fall through
    to a full install — `--dry-runn` installs, having been asked to touch nothing.
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
    # Validating tokens INDIVIDUALLY is not enough: `--dry-run --uninstall` are both
    # real flags, and --uninstall wins the dispatch below — so it would unwire
    # settings.json having been told to touch nothing. uninstall() takes no `dry`,
    # so there is nothing to honour; refuse the combination.
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
