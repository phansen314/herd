"""herd CLI — list live sessions and jump to them.

    python3 -m herd.cli ls
    python3 -m herd.cli jump <query>   # query: herd id, uuid-prefix, cwd, or job

`jump` re-derives the target's kitty window and focuses it (see herd.kitty.focus).
A unique match focuses; zero or many print the candidates so you can narrow.
"""
import sys

from herd.db import connect
from herd.daemon import DEFAULT_DB, _now_iso
from herd.kitty.focus import focus_session


def _live(conn):
    return conn.execute(
        "SELECT s.id, s.session_id, s.pid, s.status, s.cwd, s.total_cost_usd, "
        "       h.job_name, h.window_id, (a.attention_at IS NOT NULL) AS attn "
        "FROM sessions s "
        "LEFT JOIN herd_sessions  h ON h.session_pk = s.id "
        "LEFT JOIN herd_attention a ON a.session_pk = s.id "
        "WHERE s.stopped_at IS NULL "
        "ORDER BY a.attention_at IS NULL, a.attention_at, s.started_at DESC").fetchall()


def _fmt(rows):
    if not rows:
        return "  (no live sessions)"
    out = []
    for r in rows:
        uuid = (r["session_id"] or "")[:8] or "—"
        cost = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
        out.append(f"  {'!' if r['attn'] else ' '} #{r['id']:<3} {uuid:8}  "
                   f"{r['status']:14} {(r['job_name'] or '—'):12} {cost:>7}  {r['cwd']}")
    return "\n".join(out)


def resolve(conn, query):
    """Live sessions matching query: exact herd id, uuid prefix, cwd substring, or
    exact job name."""
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


def cmd_ls(conn, args):
    print(_fmt(_live(conn)))
    return 0


def cmd_jump(conn, args):
    if not args or not args[0].strip():
        print("usage: herd jump <id | uuid-prefix | cwd | job>")
        print(_fmt(_live(conn)))
        return 2
    matches = resolve(conn, args[0])
    if not matches:
        print(f"no live session matches {args[0]!r}:")
        print(_fmt(_live(conn)))
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} sessions match {args[0]!r} — narrow it:")
        print(_fmt(matches))
        return 1
    ok, msg = focus_session(conn, matches[0]["id"], _now_iso())
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


COMMANDS = {"ls": cmd_ls, "jump": cmd_jump}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "ls"
    if cmd not in COMMANDS:
        print(f"herd: unknown command {cmd!r} (try: ls, jump)")
        return 2
    conn = connect(DEFAULT_DB)
    return COMMANDS[cmd](conn, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
