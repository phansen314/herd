"""Jump to a session's kitty window — reusable by the CLI and the future TUI.

Placement (kitty_socket, window_id) is a CACHE: kitty REUSES window ids, so before
focusing we re-derive the window from `kitten @ ls` and confirm its foreground claude
carries the session's stored pid. Fall back to the stored window_id when the pid
can't be located; self-heal when it drifted.

kitty's `--match pid:N` matches the WINDOW's pid (the login shell), never the
foreground claude — so we resolve pid -> window ourselves and focus by `--match id:`.
IO (list_fn/focus_fn) is injected. See DESIGN.md#focus--jump-kittyfocuspy-clipy.
"""
import json
import os
import subprocess

from herd.db import load_statements

W = load_statements()
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")


# ── kitty IO (swappable in tests) ────────────────────────────────────────────
# A stale `unix:/tmp/kitty-<pid>` outlives the kitty that made it, so every call here
# must be bounded ourselves — kitten's own ~10s timeout is not a contract. OSError
# must be caught too: kitten absent from PATH otherwise raises FileNotFoundError
# straight out of the CLI.
KITTY_TIMEOUT = 5


def _ls(socket):
    try:
        return subprocess.run(["kitten", "@", "--to", socket, "ls"],
                              capture_output=True, text=True,
                              timeout=KITTY_TIMEOUT).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""                      # unparseable -> flatten_windows None -> fall
                                       # back to the cached window_id


def _focus(socket, window_id):
    try:
        return subprocess.run(
            ["kitten", "@", "--to", socket, "focus-window", "--match", f"id:{window_id}"],
            capture_output=True, text=True, timeout=KITTY_TIMEOUT).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── pure resolution ──────────────────────────────────────────────────────────
def flatten_windows(ls_json):
    """`kitten @ ls` tree JSON -> flat [window dict], or None if it won't parse."""
    try:
        tree = json.loads(ls_json)
    except (ValueError, TypeError):
        return None
    return [w for osw in tree for t in osw.get("tabs", []) for w in t.get("windows", [])]


def window_for_pid(windows, pid):
    """The id of the window whose foreground processes include a claude with this
    pid. None if not found."""
    for w in windows or []:
        for fp in w.get("foreground_processes", []):
            cmd = fp.get("cmdline") or [""]
            if fp.get("pid") == pid and os.path.basename(cmd[0] or "") == CLAUDE_NAME:
                return w["id"]
    return None


# ── the jump ─────────────────────────────────────────────────────────────────
def focus_session(conn, session_pk, now, *, list_fn=None, focus_fn=None):
    """Focus a live session's kitty window by its surrogate pk: re-derive the window
    from ps-in-kitty, focus it, ack its attention, self-heal a drifted window_id.
    Returns (ok, message)."""
    list_fn = list_fn or (lambda s: flatten_windows(_ls(s)))
    focus_fn = focus_fn or _focus
    row = conn.execute(
        "SELECT s.pid, h.kitty_socket, h.window_id "
        "FROM sessions s JOIN herd_sessions h ON h.session_pk = s.id "
        "WHERE s.id = ? AND s.stopped_at IS NULL", (session_pk,)).fetchone()
    if row is None:
        return False, "no live session with placement for that id"
    socket, pid, stored = row["kitty_socket"], row["pid"], row["window_id"]

    win = window_for_pid(list_fn(socket), pid) if pid is not None else None
    if win is None:
        win = stored              # fall back to the cached placement
    if win is None:
        return False, "no window to focus (session has no placement yet)"

    if not focus_fn(socket, win):
        return False, f"kitty focus failed for window {win} on {socket}"

    # a jump IS an ack: clear this session's attention if it was raised.
    conn.execute(W["W6c_ack"], {"pk": session_pk, "now": now, "focus_started_at": now})
    if win != stored:
        conn.execute("UPDATE herd_sessions SET window_id = ?, verified_at = ? "
                     "WHERE session_pk = ?", (win, now, session_pk))
    return True, f"focused window {win} on {socket}"
