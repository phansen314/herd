"""herd installer — cut over from klawde (capture only).

    python3 -m herd.install            # wire herd, unwire klawde (backs up first)
    python3 -m herd.install --uninstall  # restore the most recent backups
    python3 -m herd.install --dry-run    # show what would change, touch nothing

Idempotent. Every edited file is backed up as <file>.herd-bak.<ts> before the
first change. Reuses herd.db for the DB bootstrap. Leaves klawde's repo and
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


# ── DB bootstrap ───────────────────────────────────────────────────────────
def bootstrap_db(dry=False):
    if dry:
        return f"would create {DB} and apply core.sql + herd.sql"
    HERD_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(str(DB))
    apply_schema(conn)          # idempotent: CREATE TABLE IF NOT EXISTS
    conn.close()
    return f"bootstrapped {DB}"


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
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
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
        subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True)
    active = subprocess.run(["systemctl", "--user", "is-active", "herd.service"],
                            capture_output=True, text=True).stdout.strip()
    return f"herd.service written + enabled ({active or 'unknown'})"


def uninstall_service():
    if not _has_systemd_user() or not SERVICE.exists():
        return "no herd.service to remove"
    subprocess.run(["systemctl", "--user", "disable", "--now", "herd.service"],
                   check=False, capture_output=True)
    SERVICE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    return f"removed {SERVICE}"


# ── settings.json surgery ──────────────────────────────────────────────────
def rewire_settings(data):
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

        subprocess.run([hook_cmd("session_start.sh")],          # direct exec
                       input=f'{{"session_id":"{SID}","cwd":"/x","model":"m","source":"startup"}}',
                       capture_output=True, text=True, env=env)
        sl = subprocess.run([STATUSLINE],                        # direct exec
                            input=f'{{"session_id":"{SID}","model":{{"id":"m"}},"cwd":"/x",'
                                  '"context_window":{"used_percentage":10},"cost":{"total_cost_usd":0.5}}',
                            capture_output=True, text=True, env=env)
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

    settings = json.loads(SETTINGS.read_text())
    new_settings = rewire_settings(settings)
    wrapper_text, wrap_ok = rewire_wrapper(WRAPPER.read_text()) if WRAPPER.exists() else ("", False)

    if dry:
        print(f"  would back up + rewrite {SETTINGS}")
        print(f"  would back up + rewrite {WRAPPER} (statusline -> herd: {wrap_ok})")
        print("  " + install_service(dry=True))
        print("\n  resulting hooks:")
        for e, blocks in new_settings["hooks"].items():
            cmds = [h.get("command") or f"<{h.get('type','?')}:{h.get('url','')}>"
                    for b in blocks for h in b["hooks"]]
            print(f"    {e}: {cmds}")
        return

    backup(SETTINGS, ts)
    SETTINGS.write_text(json.dumps(new_settings, indent=2) + "\n")
    print(f"  rewired {SETTINGS} (backup: *.herd-bak.{ts})")
    if WRAPPER.exists():
        backup(WRAPPER, ts)
        WRAPPER.write_text(wrapper_text)
        print(f"  rewired {WRAPPER} statusline -> herd")

    ok, row = selftest()
    print(f"\n  self-test (wired hooks -> temp DB): {'PASS' if ok else 'FAIL'}  {row}")
    print("  " + install_service())
    print("\n  klawde is unwired but NOT deleted — ~/.klawde/sessions.db (history) is kept.")
    print("  view sessions meanwhile:")
    print('    sqlite3 ~/.herd/herd.db "SELECT s.id,h.job_name,s.cwd,s.status FROM sessions s '
          'LEFT JOIN herd_sessions h ON h.session_pk=s.id WHERE s.stopped_at IS NULL"')
    print(f"\n  restore:  python3 -m herd.install --uninstall")


def uninstall():
    """Restore the most recent backup of each edited file + remove the service."""
    print("  " + uninstall_service())
    for path in (SETTINGS, WRAPPER):
        baks = sorted(path.parent.glob(path.name + ".herd-bak.*"))
        if baks:
            shutil.copy2(baks[-1], path)
            print(f"  restored {path} from {baks[-1].name}")
        else:
            print(f"  no backup found for {path}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if "--uninstall" in argv:
        uninstall()
    else:
        install(dry="--dry-run" in argv)


if __name__ == "__main__":
    main()
