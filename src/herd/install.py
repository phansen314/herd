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
HOME = pathlib.Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
WRAPPER = HOME / ".claude" / "custom-status-line.sh"
HERD_DIR = HOME / ".herd"
DB = HERD_DIR / "herd.db"

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
    """Prove the wired session_start + statusline actually write, using a throwaway
    DB so the real one is untouched."""
    with tempfile.TemporaryDirectory(prefix="herd-selftest-") as tmp:
        env = dict(os.environ, HERD_DB=f"{tmp}/t.db", HERD_RUNTIME=tmp,
                   HERD_ERRLOG=f"{tmp}/err.log")
        connect(f"{tmp}/t.db").close()
        c = connect(f"{tmp}/t.db"); apply_schema(c); c.close()
        subprocess.run(["bash", hook_cmd("session_start.sh")],
                       input='{"session_id":"selftest","cwd":"/x","model":"m","source":"startup"}',
                       capture_output=True, text=True, env=env)
        subprocess.run(["bash", STATUSLINE],
                       input='{"session_id":"selftest","model":{"id":"m"},"cwd":"/x",'
                             '"context_window":{"used_percentage":10},"cost":{"total_cost_usd":0.5}}',
                       capture_output=True, text=True, env=env)
        c = connect(f"{tmp}/t.db")
        row = c.execute("SELECT status, context_percent FROM sessions "
                        "WHERE session_id='selftest'").fetchone()
        c.close()
        ok = row is not None and row["status"] == "working" and row["context_percent"] == 10
        return ok, dict(row) if row else None


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
    print("\n  klawde is unwired but NOT deleted — ~/.klawde/sessions.db (history) is kept.")
    print("  view sessions meanwhile:")
    print('    sqlite3 ~/.herd/herd.db "SELECT s.id,h.job_name,s.cwd,s.status FROM sessions s '
          'LEFT JOIN herd_sessions h ON h.session_pk=s.id WHERE s.stopped_at IS NULL"')
    print(f"\n  restore:  python3 -m herd.install --uninstall")


def uninstall():
    """Restore the most recent backup of each edited file."""
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
