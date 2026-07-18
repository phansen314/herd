"""herd CLI — list live sessions and jump to them.

    herd ls                 # list live sessions
    herd jump               # fuzzy-pick a session (fzf) and focus its kitty window
    herd jump <query>       # query = herd id, uuid-prefix, cwd, or job
    herd preview <id>       # session detail (used by the fzf preview pane)

`jump` focuses immediately on a unique query match (scriptable); otherwise it opens
an fzf picker with a live detail preview. Without fzf (or a tty) it prints the list.
"""
import shutil
import subprocess
import sys

from herd.db import connect
from herd.daemon import DEFAULT_DB, _now_iso
from herd.kitty.focus import focus_session


def _live(conn):
    return conn.execute(
        "SELECT s.id, s.session_id, s.pid, s.status, s.cwd, s.total_cost_usd, "
        "       h.job_name, (a.attention_at IS NOT NULL) AS attn "
        "FROM sessions s "
        "LEFT JOIN herd_sessions  h ON h.session_pk = s.id "
        "LEFT JOIN herd_attention a ON a.session_pk = s.id "
        "WHERE s.stopped_at IS NULL "
        "ORDER BY a.attention_at IS NULL, a.attention_at, s.started_at DESC").fetchall()


def _line(r):
    """The display half of a session row (no id prefix)."""
    uuid = (r["session_id"] or "")[:8] or "—"
    cost = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
    return (f"{'!' if r['attn'] else ' '} #{r['id']:<3} {uuid:8} {r['status']:14} "
            f"{(r['job_name'] or '—'):12} {cost:>7}  {r['cwd']}")


def _fmt(rows):
    return "\n".join("  " + _line(r) for r in rows) if rows else "  (no live sessions)"


def resolve(conn, query):
    """Live sessions matching query: exact herd id, uuid prefix, cwd substring, or
    exact job name. An empty query matches nothing (never all)."""
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


def _fzf_pick(rows, query):
    """Interactive fuzzy pick with a live preview pane. Returns a row or None."""
    preview = f"{sys.executable} -m herd.cli preview {{1}}"   # {1} = the hidden id
    p = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2..", "--reverse",
         "--height", "60%", "--query", query, "--prompt", "jump ▸ ",
         "--preview", preview, "--preview-window", "right,55%,wrap"],
        input="\n".join(_row_line(r) for r in rows), capture_output=True, text=True)
    return _parse_pick(rows, p.stdout)


# ── preview (its own process, spawned by fzf per highlight — reads live) ──────
def _preview_text(row):
    d = dict(row)

    def g(k, dflt="—"):
        v = d.get(k)
        return dflt if v in (None, "") else v

    cost = f"${d['total_cost_usd']:.2f}" if d.get("total_cost_usd") is not None else "—"
    ctx = f"{d['context_percent']}%" if d.get("context_percent") is not None else "—"
    lines = [
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


def cmd_preview(conn, args):
    if not args or not args[0].strip().isdigit():
        return 1
    r = conn.execute(
        "SELECT s.id, s.session_id, s.pid, s.status, s.status_source, s.model, s.cwd, "
        "       s.git_branch, s.context_percent, s.total_cost_usd, s.started_at, "
        "       s.last_event_at, s.last_event_type, h.job_name, a.attention_at, a.paged_level "
        "FROM sessions s "
        "LEFT JOIN herd_sessions  h ON h.session_pk = s.id "
        "LEFT JOIN herd_attention a ON a.session_pk = s.id "
        "WHERE s.id = ?", (int(args[0].strip()),)).fetchone()
    print(_preview_text(r) if r is not None else "(session gone)")
    return 0


COMMANDS = {"ls": cmd_ls, "jump": cmd_jump, "preview": cmd_preview}
_READONLY = {"ls", "preview"}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "ls"
    if cmd not in COMMANDS:
        print(f"herd: unknown command {cmd!r} (try: ls, jump, preview)")
        return 2
    conn = connect(DEFAULT_DB, readonly=cmd in _READONLY)
    return COMMANDS[cmd](conn, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
