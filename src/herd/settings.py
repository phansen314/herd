"""THE one definition of what herd owns inside ~/.claude/settings.json.

install and doctor both import from here; they must not answer "is this hook ours?"
differently. The walkers tolerate any shape a hand edit can leave behind, because
they run against a file herd did not write and may be invoked to repair.
"""
import pathlib

HOOKS_DIR = pathlib.Path(__file__).resolve().parent / "hooks"   # in the checkout
HERD_DIR = pathlib.Path.home() / ".herd"
INSTALLED_HOOKS = HERD_DIR / "hooks"                            # what actually runs

# event -> (hook script, async?). SessionStart and SessionEnd must stay BLOCKING:
# async SessionEnd loses the final write.
HERD_HOOKS = {
    "SessionStart":     ("session_start.sh", False),
    "Stop":             ("stop.sh",          True),
    "SessionEnd":       ("session_end.sh",   False),
    "Notification":     ("notification.sh",  True),
    "PostToolUse":      ("post_tool_use.sh", True),
    # Tier-2 enrichment: capture the live kitty tab title. Async — best-effort, must
    # not block the prompt. Runs alongside the tier-1 hooks, never inside them.
    "UserPromptSubmit": ("tab_sync.sh",      True),
}

OUR_SCRIPTS = {s for s, _ in HERD_HOOKS.values()} | {"statusline.sh", "common.sh"}


def hook_cmd(script, hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / script)


def statusline_cmd(hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / "statusline.sh")


def default_roots():
    """The two acceptable hook locations: the installed copy and the checkout."""
    return (HOOKS_DIR, INSTALLED_HOOKS)


def is_managed(cmd, roots=None):
    """A command herd owns — klawde's, or any prior herd install. Everything else
    (cdh, the PreToolUse HTTP hook, anything unknown) is preserved untouched.

    Matches by NAME as well as by path prefix: a prefix test alone misses an install
    made from a moved checkout, whose stale entry then survives the strip and gets
    duplicated on re-add. The herd-shaped-path condition stops us claiming an
    unrelated tool's session_start.sh."""
    if not cmd:
        return False
    if "/.klawde/" in cmd:
        return True
    # roots is a PARAMETER: callers (and tests) redirect INSTALLED_HOOKS, and
    # resolving against this module's constants would ignore the redirect.
    for r in (roots if roots is not None else default_roots()):
        if cmd.startswith(str(r)):
            return True
    parts = cmd.split()
    if not parts:                       # whitespace-only: `not cmd` misses it, and
        return False                    # split()[0] on it is an IndexError
    path = parts[0]
    return path.rsplit("/", 1)[-1] in OUR_SCRIPTS and ("/herd/" in path or "/.herd/" in path)


def hook_commands(data):
    """[(event, command), ...] from a settings dict. Every level is type-checked
    because every level can be hand-edited into something else."""
    out = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            hs = b.get("hooks")
            if not isinstance(hs, list):
                continue
            for h in hs:
                if isinstance(h, dict):
                    out.append((event, h.get("command") or ""))
    return out


def statusline_command(data):
    """settings.statusLine.command, or "" for any other shape."""
    if not isinstance(data, dict):
        return ""
    sl = data.get("statusLine")
    return (sl.get("command") or "") if isinstance(sl, dict) else ""


def strip_managed(hooks, roots=None):
    """Remove every herd-managed command IN PLACE, dropping blocks and then events
    that become empty. cdh / PreToolUse-HTTP / others are untouched.

    Shared by rewire_settings and unwire_settings: a strip that drifts from the
    re-add either doubles every hook or strands one behind. Shape-tolerant like
    hook_commands, and anything unrecognised is left ALONE rather than dropped —
    herd does not own a shape it cannot read."""
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        blocks = hooks[event]
        if not isinstance(blocks, list):
            continue                    # not ours to interpret; leave it untouched
        for block in blocks:
            if not isinstance(block, dict):
                continue
            hs = block.get("hooks")
            if not isinstance(hs, list):
                continue
            block["hooks"] = [h for h in hs
                              if not (isinstance(h, dict)
                                      and is_managed(h.get("command") or "", roots))]
        # An unparseable block is KEPT — treating it as empty deletes a stranger's entry.
        kept = [b for b in blocks
                if not isinstance(b, dict) or b.get("hooks")]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
