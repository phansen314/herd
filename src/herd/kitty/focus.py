"""Jump to a session's kitty window — the focus path, reusable by the CLI and the
future TUI.

The hooks store placement (kitty_socket, window_id), but window_id is a CACHE. Before
focusing we RE-DERIVE the window from `kitten @ ls` and confirm the window's
foreground claude carries the session's stored pid — so we never focus a window that
was reused after our session's window closed. We fall back to the stored window_id
when the pid can't be located, and self-heal the stored value when it has drifted.

kitty's own `--match pid:N` matches the WINDOW's pid (the login shell), NEVER the
foreground claude (measured: claude's pid matches nothing, the shell's matches the
window). So we resolve pid -> window ourselves and focus by `--match id:`.

IO (list_fn / focus_fn) is injected so the logic is testable without a live kitty —
the same discipline as daemon.py and hooks/common.sh.
"""
import json
import os
import subprocess

from herd.db import load_statements

W = load_statements()
CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")


# ── kitty IO (swappable in tests) ────────────────────────────────────────────
def _ls(socket):
    return subprocess.run(["kitten", "@", "--to", socket, "ls"],
                          capture_output=True, text=True).stdout


def _focus(socket, window_id):
    return subprocess.run(
        ["kitten", "@", "--to", socket, "focus-window", "--match", f"id:{window_id}"],
        capture_output=True, text=True).returncode == 0


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
    pid — the exact re-derivation. None if not found."""
    for w in windows or []:
        for fp in w.get("foreground_processes", []):
            cmd = fp.get("cmdline") or [""]
            if fp.get("pid") == pid and os.path.basename(cmd[0] or "") == CLAUDE_NAME:
                return w["id"]
    return None


# ── the jump ─────────────────────────────────────────────────────────────────
def focus_session(conn, session_pk, now, *, list_fn=None, focus_fn=None):
    """Focus a live session's kitty window by its surrogate pk. Re-derives the
    window from ps-in-kitty, focuses it, acks its attention (a jump means "I've seen
    it"), and self-heals a drifted window_id. Returns (ok, message)."""
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
    # self-heal the cache when the re-derived window differs from what was stored.
    if win != stored:
        conn.execute("UPDATE herd_sessions SET window_id = ?, verified_at = ? "
                     "WHERE session_pk = ?", (win, now, session_pk))
    return True, f"focused window {win} on {socket}"
