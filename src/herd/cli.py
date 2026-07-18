"""herd CLI — list live sessions and jump to them.

    herd ls                 # list live sessions
    herd jump               # fuzzy-pick a session (fzf) and focus its kitty window
    herd jump <query>       # query = herd id, name (/rename), job, uuid, or cwd
    herd watch              # the picker as a permanent dashboard (dedicated tab)
    herd preview <id>       # session detail (used by the fzf preview pane)

Sessions show by their recognizable name — Claude's /rename name, else herd's job,
else the uuid. `jump` focuses immediately on a unique query match (scriptable);
otherwise it opens an fzf picker with a live detail preview. Without fzf (or a tty)
it prints the list.

There is deliberately NO TUI. `watch` is the whole dashboard: fzf already renders the
list, navigates it, and shows live per-session detail in its preview pane, so a curses
layer would only be a second rendering path to keep in sync with `ls`. See cmd_watch.
"""
import os
import shutil
import subprocess
import sys

from herd.db import connect, load_statements
from herd.daemon import DEFAULT_DB, _now_iso
from herd.kitty.focus import focus_session

_HOME = os.path.expanduser("~")


_STMT = load_statements()


def _live(conn):
    """The one live-session read: writes.sql's R1_list, not a private transcription.
    It carries every column ls, the picker and the preview pane need."""
    return conn.execute(_STMT["R1_list"]).fetchall()


def _name(r):
    """The recognizable label: Claude's /rename name, else herd's job, else uuid8."""
    return r["session_name"] or r["job_name"] or (r["session_id"] or "")[:8] or "—"


def _short_cwd(cwd):
    cwd = cwd or ""
    return "~" + cwd[len(_HOME):] if cwd.startswith(_HOME) else cwd


def _line(r):
    """The display half of a session row (no id prefix)."""
    name = _name(r)
    name = name[:25] + "…" if len(name) > 26 else name
    cost = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
    return (f"{'!' if r['attention_at'] else ' '} #{r['id']:<3} {name:26} {r['status']:14} "
            f"{cost:>8}  {_short_cwd(r['cwd'])}")


def _fmt(rows):
    return "\n".join("  " + _line(r) for r in rows) if rows else "  (no live sessions)"


def resolve(conn, query):
    """Live sessions matching query: exact herd id, uuid prefix, session-name or
    cwd substring, or exact job name. An empty query matches nothing (never all)."""
    q = query.strip()
    if not q:
        return []
    rows = _live(conn)
    if q.isdigit():
        exact = [r for r in rows if r["id"] == int(q)]
        if exact:
            return exact
    ql = q.lower()
    return [r for r in rows
            if (r["session_id"] or "").startswith(q)
            or ql in (r["session_name"] or "").lower()
            or ql in (r["cwd"] or "").lower()
            or (r["job_name"] or "") == q]


# ── fzf picker ───────────────────────────────────────────────────────────────
def _row_line(r):
    """`<id>\\t<display>` — id is a hidden first field fzf keeps for parsing +
    preview but hides from the list (--with-nth=2..)."""
    return f"{r['id']}\t{_line(r)}"


def _parse_pick(rows, fzf_stdout):
    """The row fzf returned (by its leading id), or None on empty/garbage/cancel."""
    head = fzf_stdout.split("\t", 1)[0].strip()
    if not head.isdigit():
        return None
    sid = int(head)
    return next((r for r in rows if r["id"] == sid), None)


def _has_fzf():
    return bool(shutil.which("fzf")) and sys.stdin.isatty()


def _fzf_pick(rows, query, extra=()):
    """Interactive fuzzy pick with a live preview pane. Returns a row or None.

    `extra` appends watch-mode flags (--listen, the poker, extra binds) without
    forking a second copy of the flag list — jump and watch must not drift apart on
    --delimiter/--with-nth/--preview, which _parse_pick depends on.

    Capture ONLY stdout (the selection). fzf draws its UI to stderr/the tty — piping
    stderr (capture_output=True) makes the picker invisible and looks like a hang.
    """
    preview = f"{sys.executable} -m herd.cli preview {{1}}"   # {1} = the hidden id
    p = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2..", "--reverse",
         "--height", "60%", "--query", query, "--prompt", "jump ▸ ",
         "--preview", preview, "--preview-window", "right,55%,wrap", *extra],
        input="\n".join(_row_line(r) for r in rows),
        stdout=subprocess.PIPE, text=True)   # stderr stays on the terminal (fzf's UI)
    return _parse_pick(rows, p.stdout)


# ── preview (its own process, spawned by fzf per highlight — reads live) ──────
def _preview_text(row):
    d = dict(row)

    def g(k, dflt="—"):
        v = d.get(k)
        return dflt if v in (None, "") else v

    cost = f"${d['total_cost_usd']:.2f}" if d.get("total_cost_usd") is not None else "—"
    ctx = f"{d['context_percent']}%" if d.get("context_percent") is not None else "—"
    name = d.get("session_name") or d.get("job_name") or (d.get("session_id") or "")[:8] or "—"
    lines = [
        f"name      {name}",
        f"session   {g('session_id')}",
        f"herd id   #{d.get('id', '—')}",
        f"status    {g('status')}" + (f"  ({d['status_source']})" if d.get("status_source") else ""),
        f"model     {g('model')}",
        f"job       {g('job_name')}",
        f"pid       {g('pid')}",
        f"cwd       {g('cwd')}",
        f"branch    {g('git_branch')}",
        f"context   {ctx}",
        f"cost      {cost}",
        f"started   {g('started_at')}",
        f"last      {g('last_event_at')}  ({g('last_event_type')})",
    ]
    if d.get("attention_at"):
        lines.append(f"⚠ needs attention since {d['attention_at']}  (rung {d.get('paged_level') or 0})")
    return "\n".join(lines)


# ── commands ─────────────────────────────────────────────────────────────────
def _do_focus(conn, row):
    ok, msg = focus_session(conn, row["id"], _now_iso())
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_ls(conn, args):
    print(_fmt(_live(conn)))
    return 0


def cmd_jump(conn, args):
    query = args[0].strip() if (args and args[0].strip()) else None
    rows = _live(conn)
    if not rows:
        print("  (no live sessions)")
        return 1
    if query:
        matches = resolve(conn, query)
        if len(matches) == 1:                 # unambiguous -> just go (scriptable)
            return _do_focus(conn, matches[0])
        candidates = matches or rows          # 0 matches: pick over all, seeded
    else:
        candidates = rows
    if _has_fzf():
        picked = _fzf_pick(candidates, query or "")
        return _do_focus(conn, picked) if picked is not None else 130   # cancel = quiet
    # no fzf / not a tty: printed fallback
    if query and not resolve(conn, query):
        print(f"no live session matches {query!r}:")
    print(_fmt(candidates))
    return 0 if candidates else 1


def cmd_rows(conn, args):
    """The picker's list on stdout — same text _fzf_pick pipes in, for fzf's reload."""
    print("\n".join(_row_line(r) for r in _live(conn)))
    return 0


# ── watch: the picker as a permanent dashboard (fzf IS the TUI — see DESIGN.md) ──
_ROWS_CMD = f"{sys.executable} -m herd.cli rows"
_POKE_INTERVAL = 2.0
_POKE_GRACE = 10                            # ticks to let fzf bind before giving up


def _rows_text(conn):
    return "\n".join(_row_line(r) for r in _live(conn))


def _poke_loop(conn, port, send, sleep, rounds=None):
    """Poll the rows; send `reload` only when they CHANGE. Injected IO, like focus.py.

    Change-gated to spare the pane needless redraws. But contact fzf EVERY tick even
    when nothing changed (data=None -> a liveness GET): watch respawns a poker per
    picker, so a poker that only spoke on change would outlive its fzf forever on an
    idle herd and pile up one process per jump. Returns why it stopped (for tests).
    """
    last, n, up = _rows_text(conn), 0, False   # seed: fzf already has these on stdin
    while rounds is None or n < rounds:
        n += 1
        sleep(_POKE_INTERVAL)
        try:
            cur = _rows_text(conn)
        except Exception:
            return "db"                     # DB gone mid-write: don't kill the pane
        changed, last = cur != last, cur
        try:
            send(f"http://localhost:{port}",
                 f"reload({_ROWS_CMD})".encode() if changed else None)
            up = True
        except Exception:
            # Before the first success this is fzf still binding, not fzf gone —
            # measured: watch spawns us first, and exiting here killed auto-refresh
            # outright. After it, a failure means the port closed.
            if up or n >= _POKE_GRACE:
                return "gone"               # fzf exited / port closed -> reap poker
    return "done"


def _http_send(url, data):
    """POST an action, or GET (data=None) purely to check fzf is still listening."""
    import urllib.request                   # stdlib, not curl — herd has no runtime deps
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=2).close()


def cmd_poke(conn, args):
    """Background child of the picker. fzf exports $FZF_PORT, so --listen=0 can take
    a free port and we never collide on a fixed one."""
    import time
    port = os.environ.get("FZF_PORT")
    if not port:
        return 1
    _poke_loop(conn, port, _http_send, time.sleep)
    return 0


def _free_port():
    """Pick the listen port ourselves rather than `--listen=0`.

    fzf's `start` event does NOT reliably see $FZF_PORT — measured: the poker spawned
    on one picker and not the next, so auto-refresh worked intermittently. Choosing the
    port here makes watch the poker's parent instead, so it can spawn it deterministically
    AND reap it, rather than leaving an orphan to notice fzf's death on its own.
    """
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _watch_flags(port):
    return [f"--listen={port}",
            f"--bind=ctrl-r:reload({_ROWS_CMD})",
            "--bind=ctrl-q:abort",
            "--prompt", "herd ▸ ",
            "--header", "enter jump · ctrl-r refresh · ctrl-q quit"]


def _spawn_poker(port):
    return subprocess.Popen([sys.executable, "-m", "herd.cli", "poke"],
                            env={**os.environ, "FZF_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_watch(conn, args):
    """The jump picker, looping forever, self-refreshing — a tab you can't fall out of.
    Esc re-enters the picker; ctrl-q (or ctrl-c) is the way out."""
    import time
    if not _has_fzf():
        print("herd watch needs fzf and a tty (try: herd ls)")
        return 2
    empty = False
    while True:
        rows = _live(conn)
        if not rows:
            if not empty:                   # print once, not every tick
                print("  (no live sessions — waiting)")
                empty = True
            time.sleep(_POKE_INTERVAL)
            continue
        empty = False
        port = _free_port()
        poker = _spawn_poker(port)
        try:
            picked = _fzf_pick(rows, "", _watch_flags(port))
        except KeyboardInterrupt:
            return 130
        finally:
            poker.terminate()               # one poker per picker, always reaped
        if picked is not None:
            _do_focus(conn, picked)         # pick or cancel: either way, back in


def _complete_tokens(rows):
    """Completion candidates for `herd jump` — the things resolve() matches on:
    each live session's name (/rename), job, 8-char uuid, and cwd basename."""
    toks = set()
    for r in rows:
        if r["session_name"]:
            toks.add(r["session_name"])
        if r["job_name"]:
            toks.add(r["job_name"])
        if r["session_id"]:
            toks.add(r["session_id"][:8])
        cwd = (r["cwd"] or "").rstrip("/")
        if cwd:
            toks.add(cwd.rsplit("/", 1)[-1] or cwd)
    return sorted(toks)


def cmd_complete(conn, args):
    print("\n".join(_complete_tokens(_live(conn))))
    return 0


def cmd_preview(conn, args):
    """Detail for one id, picked out of the same R1_list read the list uses — a
    session that died while the picker was open correctly reads "(session gone)"."""
    if not args or not args[0].strip().isdigit():
        return 1
    sid = int(args[0].strip())
    r = next((r for r in _live(conn) if r["id"] == sid), None)
    print(_preview_text(r) if r is not None else "(session gone)")
    return 0


COMMANDS = {"ls": cmd_ls, "jump": cmd_jump, "watch": cmd_watch,
            "preview": cmd_preview, "complete": cmd_complete, "rows": cmd_rows,
            "poke": cmd_poke}
# `watch` focuses windows (via _do_focus), so it is NOT readonly.
_READONLY = {"ls", "preview", "complete", "rows", "poke"}
# The verbs a user actually types. `preview` (fzf's per-highlight pane), `complete`
# (tab-completion feed), `rows` (fzf's reload source) and `poke` (watch's refresh
# child) are machinery — callable, but not advertised in help or tab-completion.
USER_COMMANDS = ("ls", "jump", "watch")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "ls"
    if cmd not in COMMANDS:
        print(f"herd: unknown command {cmd!r} (try: {', '.join(USER_COMMANDS)})")
        return 2
    conn = connect(DEFAULT_DB, readonly=cmd in _READONLY)
    return COMMANDS[cmd](conn, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
