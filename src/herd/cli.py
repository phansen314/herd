"""herd CLI — list live sessions and jump to them.

    herd ls                 # list live sessions
    herd jump               # fuzzy-pick a session (fzf) and focus its kitty window
    herd jump <query>       # query = herd id, name (/rename), job, uuid, or cwd
    herd spawn <job>        # launch a named claude session in a kitty tab/pane
    herd watch              # the picker as a permanent dashboard (dedicated tab)
    herd watch --one-shot   # same picker, exits after one jump (kitty overlay panel)
    herd preview <id>       # session detail (used by the fzf preview pane)

Sessions show by their recognizable name — Claude's /rename name, else herd's job,
else the uuid. `jump` focuses immediately on a unique query match (scriptable);
otherwise it opens an fzf picker with a live detail preview. Without fzf (or a tty)
it prints the list.

There is deliberately NO TUI. `watch` is the whole dashboard: fzf already renders the
list, navigates it, and shows live per-session detail in its preview pane, so a curses
layer would only be a second rendering path to keep in sync with `ls`. See cmd_watch.
"""
import argparse
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

from herd.db import connect, load_statements
from herd.daemon import DEFAULT_DB, _now_iso
from herd.kitty.focus import focus_session
from herd.spawn import resolve_spec, spawn
from herd.template import load_template, available_templates

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


def _unacked(r):
    """Mark only attention nobody has looked at yet. A jump acks the row (W6c)
    without deleting it, so an acked row stays armed and stays quiet; the daemon
    re-arms it from ack_at once that silence runs long again."""
    return bool(r["attention_at"]) and not r["ack_at"]


# The mark says WHICH KIND of attention, because they are not equivalent: Claude
# rings the terminal bell for `waiting` and `needs_approval` (it ends a turn or
# raises a prompt), but a session stuck in `working` never ends its turn, so
# NOTHING pushes it at you. 🥱 is the only one you can miss by not looking.
# See README#notifications-kitty-tab-bell and DECISIONS.md#pager.
# Every glyph MUST be two terminal cells wide or the column goes ragged for that
# row alone — enforced by test_source_invariants.test_attention_glyphs_are_two_cells.
ATTENTION_MARKS = {
    "waiting":        "🙋",   # turn ended, wants you
    "needs_approval": "🔐",   # blocked on a permission prompt
    "working":        "🥱",   # silently stuck: past HERD_STUCK_SECS with no activity
}
MARK_UNKNOWN = "❗"           # armed under a status we don't have a glyph for
MARK_NONE = "  "             # quiet: unarmed, or armed-but-acked

# The preview pane has room to say it in words. Same keys as ATTENTION_MARKS.
ATTENTION_REASONS = {
    "waiting":        "waiting for you",
    "needs_approval": "needs approval",
    "working":        "stuck — no activity",
}


def _mark_for(status):
    """The glyph a page-worthy status earns. needs_attention() only ever arms the
    three above, but status can drift between 2s ticks, so an armed row with an
    unexpected status must still render something rather than blow up the picker."""
    return ATTENTION_MARKS.get(status, MARK_UNKNOWN)


def _mark(r):
    """The two-cell attention flag for a row. Quiet rows still occupy the column."""
    return _mark_for(r["status"]) if _unacked(r) else MARK_NONE


def _line(r):
    """The display half of a session row (no id prefix)."""
    name = _name(r)
    name = name[:25] + "…" if len(name) > 26 else name
    cost = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
    return (f"{_mark(r)} #{r['id']:<3} {name:26} {r['status']:14} "
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


# The CHECKOUT copy, deliberately — NOT ~/.herd/hooks. The installed copy exists so
# a `git checkout` cannot change what RUNNING CLAUDE SESSIONS execute; this pane is
# spawned by a herd process that already runs checkout code and already read
# writes.sql from the checkout via load_statements(). Pointing it at ~/.herd would
# let the list and its own preview read two different R1_list definitions.
_PREVIEW_SH = pathlib.Path(__file__).resolve().parent / "hooks" / "preview.sh"


def _preview_cmd():
    """The pane's command, re-run by fzf on EVERY highlight change.

    bash+sqlite (~6ms) instead of the python verb (~78ms, ~60 of it bare
    interpreter startup — nothing inside cmd_preview was the problem). The verb
    stays as the fallback: a pip/zip install can drop the mode bit, and a blank
    preview pane is a bad way to find that out. Invoked as argv[0], not
    `bash <path>`, so a lost +x lands here rather than silently no-oping.

    fzf runs this through `sh -c`, so the PATH needs shell quoting — a checkout
    (previously a venv) under a directory with a space silently broke the pane and
    ctrl-r refresh. `{1}` stays UNQUOTED: fzf quotes placeholder substitutions
    itself, so quoting here would nest them.
    """
    if os.access(_PREVIEW_SH, os.X_OK):
        return f"{shlex.quote(str(_PREVIEW_SH))} {{1}}"
    return f"{shlex.quote(sys.executable)} -m herd.cli preview {{1}}"


def _fzf_run(rows, query, extra=()):
    """Run the picker, return (raw stdout, exit code). watch needs the raw text:
    with --expect the pressed key is the first line, and that is its only way to
    tell quit from cancel.

    THE EXIT CODE IS NOT OPTIONAL, it is the only thing that separates "the user
    pressed Esc" (130) from "fzf never ran" (2). Both produce empty stdout, so
    discarding the code left cmd_watch's loop unable to tell a self-paced human
    from a picker failing instantly — and it re-entered with no delay, binding a
    port and forking a poker each pass: 5000 iterations in 0.25s, measured.

    `extra` appends watch-mode flags (--listen, --expect, extra binds) without
    forking a second copy of the flag list — jump and watch must not drift apart on
    --delimiter/--with-nth/--preview, which _parse_pick depends on.

    Capture ONLY stdout (the selection). fzf draws its UI to stderr/the tty — piping
    stderr (capture_output=True) makes the picker invisible and looks like a hang.
    """
    preview = _preview_cmd()
    p = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2..", "--reverse",
         "--height", "60%", "--query", query, "--prompt", "jump ▸ ",
         "--preview", preview, "--preview-window", "right,55%,wrap", *extra],
        input="\n".join(_row_line(r) for r in rows),
        stdout=subprocess.PIPE, text=True)   # stderr stays on the terminal (fzf's UI)
    return p.stdout, p.returncode


def _fzf_pick(rows, query, extra=()):
    """Interactive fuzzy pick with a live preview pane. Returns a row or None."""
    return _parse_pick(rows, _fzf_run(rows, query, extra)[0])


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
    if d.get("attention_at") and not d.get("ack_at"):      # acked -> armed but quiet
        lines.append(f"{_mark_for(d.get('status'))} "
                     f"{ATTENTION_REASONS.get(d.get('status'), 'needs attention')} "
                     f"since {d['attention_at']}")
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
_ROWS_CMD = f"{shlex.quote(sys.executable)} -m herd.cli rows"   # -> sh -c, see _fzf_run
_POKE_INTERVAL = 2.0
_POKE_GRACE = 10                            # ticks to let fzf bind before giving up

# A picker that returns NOTHING in under this long did not host a human decision.
# Esc is self-paced, so re-entering instantly is right for it; an fzf that cannot
# start returns in ~1ms and re-entering instantly is a fork bomb. Time is the
# backstop behind the exit code, for a failure that exits 0 or 1 rather than 2.
_PICKER_MIN_SECS = 0.25
_PICKER_MAX_FAST = 5


def _rows_text(conn):
    return "\n".join(_row_line(r) for r in _live(conn))


def _runtime_dir():
    """Same anchor as daemon.lock_path() and the hooks' HERD_RUNTIME — one
    definition, in config.py. The picker's rows file lands here and fzf reads it, so
    a CLI that disagreed with the hooks would hand fzf a path nothing writes."""
    from herd import config as _config
    return _config.runtime_dir()


def _rows_file(port):
    """Per-picker handoff file. Keyed by the port because `watch` already chose one
    uniquely per picker (_free_port), so two dashboards cannot share it."""
    return os.path.join(_runtime_dir(), f"herd-rows-{port}")


def _write_rows(path, text):
    """tmp + replace: fzf may `cat` this at any moment, and a torn read would draw a
    half list. Same discipline as statusline.sh's cache and install._atomic_write."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text + "\n" if text else "")
    os.replace(tmp, path)


def _poke_loop(conn, port, send, sleep, rounds=None):
    """Poll the rows; send `reload` only when they CHANGE. Injected IO, like focus.py.

    Change-gated to spare the pane needless redraws. But contact fzf EVERY tick even
    when nothing changed (data=None -> a liveness GET): watch respawns a poker per
    picker, so a poker that only spoke on change would outlive its fzf forever on an
    idle herd and pile up one process per jump. Returns why it stopped (for tests).

    THE RELOAD READS A FILE, NOT A FRESH INTERPRETER. This loop already computes the
    row text in-process to detect the change; telling fzf to run
    `python -m herd.cli rows` made it start an interpreter and compute the
    byte-identical text a second time — 79ms per refresh, measured, against ~1ms for
    a `cat`. Handing over the text we already hold is also the reason there is still
    exactly ONE row formatter: a bash port would have been a second implementation
    of _line(), which `herd ls` still uses, with awk padding non-ASCII names by
    bytes or characters depending on the awk. ctrl-r deliberately keeps the python
    command — see _watch_flags.
    """
    rows_file = _rows_file(port)
    last, n, up = _rows_text(conn), 0, False   # seed: fzf already has these on stdin
    try:
        while rounds is None or n < rounds:
            n += 1
            sleep(_POKE_INTERVAL)
            try:
                cur = _rows_text(conn)
            except Exception:
                return "db"                 # DB gone mid-write: don't kill the pane
            changed, last = cur != last, cur
            try:
                if changed:
                    _write_rows(rows_file, cur)
                send(f"http://localhost:{port}",
                     f"reload(cat {shlex.quote(rows_file)})".encode() if changed else None)
                up = True
            except Exception:
                # Before the first success this is fzf still binding, not fzf gone —
                # measured: watch spawns us first, and exiting here killed auto-refresh
                # outright. After it, a failure means the port closed.
                if up or n >= _POKE_GRACE:
                    return "gone"           # fzf exited / port closed -> reap poker
        return "done"
    finally:
        # Every exit path, including watch's terminate(). A per-process file with no
        # reaper accumulates — the herd-db-err.$$ lesson.
        try:
            os.unlink(rows_file)
        except OSError:
            pass


def _http_send(url, data):
    """POST an action, or GET (data=None) purely to check fzf is still listening."""
    import urllib.request                   # stdlib, not curl — herd has no runtime deps
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=2).close()


def cmd_poke(conn, args):
    """Background child of the picker, reading the port from $FZF_PORT.

    `watch` chooses the port itself (_free_port) and passes it in via --listen and
    this variable. The obvious alternative — `--listen=0` plus fzf's own $FZF_PORT
    on a `start:` bind — was measured NOT to work: the start event does not reliably
    see the variable, so auto-refresh worked on one picker and not the next. See
    DECISIONS.md#poker."""
    import time
    port = (os.environ.get("FZF_PORT") or "").strip()
    # isdigit, because the port is not only a URL: _rows_file interpolates it into
    # a PATH, so FZF_PORT=../../x would write and then unlink outside the runtime
    # dir. watch always sets it from _free_port, so this is a guard on a value we
    # control, not a hole — but the file write is the kind that deserves one.
    if not port.isdigit():
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


_QUIT_KEYS = ("ctrl-q", "ctrl-c")


def _watch_flags(port, one_shot=False):
    """Only the header differs between the modes. The picker itself is identical —
    one-shot changes what cmd_watch does with the RESULT, not how fzf behaves, so
    --expect must stay on both paths (see test_only_watch_expects_keys)."""
    return [f"--listen={port}",
            f"--bind=ctrl-r:reload({_ROWS_CMD})",
            f"--expect={','.join(_QUIT_KEYS)}",
            "--prompt", "herd ▸ ",
            "--header", ("enter jump · esc dismiss · ctrl-r refresh" if one_shot
                         else "enter jump · ctrl-r refresh · ctrl-q quit")]


def _parse_expect(stdout):
    """--expect puts the pressed key on line 1, the selection on line 2. Returns
    (key, selection); key is "" for a plain enter, and both are "" when fzf was
    cancelled (Esc prints nothing at all)."""
    key, _, rest = stdout.partition("\n")
    return key.strip(), rest


def _spawn_poker(port):
    return subprocess.Popen([sys.executable, "-m", "herd.cli", "poke"],
                            env={**os.environ, "FZF_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reap_poker(poker, port):
    """Stop the poker AND clean up after it. Never raises — this runs in a finally.

    The unlink is here, not only in _poke_loop's finally, because that finally
    CANNOT RUN on the path that actually happens: terminate() is SIGTERM, whose
    default disposition kills the interpreter without unwinding. Verified — the
    rows file survived every terminate, so watch left one herd-rows-<port> behind
    per picker, each with a fresh random port, in the runtime dir. That is the
    accumulation the finally was written to prevent.

    wait() as well as terminate(): without it each iteration leaves a defunct child
    until the next Popen happens to reap it, so an idle dashboard sits on a zombie.
    """
    try:
        poker.terminate()
        poker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        poker.kill()                        # ignoring SIGTERM: stop being polite
        try:
            poker.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass                                # already gone; nothing to reap
    try:
        os.unlink(_rows_file(port))
    except OSError:
        pass                                # never created, or the poker got there


def cmd_watch(conn, args):
    """The jump picker, looping forever, self-refreshing — a tab you can't fall out of.
    Esc re-enters the picker; ctrl-q (or ctrl-c) is the way out.

    --one-shot keeps every bit of that machinery (the refresh poker, --listen,
    ctrl-r, the live preview pane) but exits after ONE resolution, because it runs
    in a kitty overlay window and an overlay dies with its process. Without it the
    overlay survives the jump and strands itself on the window you jumped AWAY from,
    and the next keypress — now in a different window — opens a second one. Overlays
    accumulate, one per window visited. Esc dismisses there rather than re-entering:
    a panel you cannot close is a bug, even though for the dedicated tab it was the
    whole point.
    """
    import time
    one_shot = args == ["--one-shot"]
    if args and not one_shot:
        # main() refuses this first (before opening the DB) for CLI callers; this is
        # the same guard for direct callers. The two must stay in agreement.
        print(f"herd watch: unknown option {args[0]!r} — the only option is --one-shot")
        return 2
    if not _has_fzf():
        print("herd watch needs fzf and a tty (try: herd ls)")
        return 2
    empty = False
    fast_empty = 0
    while True:
        rows = _live(conn)
        if not rows:
            if one_shot:                    # nothing to pick, and an overlay that
                print("  (no live sessions)")   # sleeps forever is a stuck panel
                return 1                    # 1, as cmd_jump returns for the same case
            if not empty:                   # print once, not every tick
                print("  (no live sessions — waiting)")
                empty = True
            # THE one place ctrl-c arrives as a signal. While the picker is up
            # fzf's raw mode disables ISIG, so ctrl-c is a KEY there (hence
            # _QUIT_KEYS) and this handler cannot fire — the opposite of what the
            # comment on it used to claim. Unguarded, waiting on an empty herd and
            # pressing ctrl-c gave a traceback.
            try:
                time.sleep(_POKE_INTERVAL)
            except KeyboardInterrupt:
                return 130
            continue
        empty = False
        port = _free_port()
        poker = _spawn_poker(port)
        started = time.monotonic()
        try:
            out, rc = _fzf_run(rows, "", _watch_flags(port, one_shot))
        except KeyboardInterrupt:
            return 130                      # belt-and-braces: a ctrl-c that lands
                                            # before fzf has taken the terminal
        finally:
            _reap_poker(poker, port)        # one poker per picker, always reaped
        # fzf: 0 selected, 1 no match, 2 ERROR, 130 interrupted (Esc/ctrl-c). Only 2
        # means the picker itself failed — a --bind this fzf does not know, a --listen
        # port taken between _free_port and exec, a terminal too small for --height.
        # Looping on that re-binds a port and forks a poker per pass with nothing
        # slowing it down. It cannot fix itself, so say why and stop.
        if rc == 2:
            print(f"herd watch: fzf exited {rc} — it could not start "
                  f"(try: herd ls, or check your fzf version supports --listen)")
            return 2
        elapsed = time.monotonic() - started
        key, sel = _parse_expect(out)
        if key in _QUIT_KEYS:
            return 0
        # Resolve against a FRESH read, not the seed `rows` this picker opened with.
        # The poker reloads the pane from the rows file and ctrl-r reloads from
        # `herd.cli rows`, so by the time enter is pressed the list on screen can
        # hold sessions the seed never had — and looking those up in the seed
        # returned None, which the `is not None` below swallowed. Enter on a
        # session that appeared while the dashboard was open did NOTHING: no focus,
        # no message. watch's own auto-refresh made its results unselectable.
        picked = _parse_pick(_live(conn), sel)
        if picked is not None:
            _do_focus(conn, picked)         # pick or cancel: either way, back in
            if one_shot:
                return 0                    # focused; the overlay tears down here.
                                            # Measured: kitty does NOT hand focus
                                            # back to the overlay's parent window on
                                            # teardown, so the jump survives it.
        elif sel.strip():                   # a selection we could not resolve at all
            print("✗ that session is gone")  # (it ended between the pick and now)
            # Deliberately NOT an exit in one-shot mode. Anything printed as an
            # overlay closes is unreadable — it flashes and the window is gone. So
            # one-shot exits on a successful focus, on Esc, or on a quit key; a pick
            # that resolves to nothing keeps the panel up so the message can be read.
        elif one_shot:
            return 130                      # Esc: no key, no selection — dismiss

        # Nothing came back AND it came back too fast to have been a keypress. One of
        # those is Esc on a quick hand; _PICKER_MAX_FAST in a row is a picker that is
        # not running. Counts CONSECUTIVE passes and resets below, so a genuine Esc
        # anywhere in the sequence clears it.
        if not key and not sel.strip() and elapsed < _PICKER_MIN_SECS:
            fast_empty += 1
            if fast_empty >= _PICKER_MAX_FAST:
                print(f"herd watch: the picker exited immediately "
                      f"{_PICKER_MAX_FAST} times running (last rc={rc}) — giving up "
                      f"rather than respawning it (try: herd ls)")
                return 2
        else:
            fast_empty = 0


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


def _split_dashdash(args):
    """Split argv on the first standalone '--': (herd_flags, claude_args). Without
    a '--', everything is herd flags and claude_args is empty."""
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i + 1:]
    return list(args), []


def cmd_spawn(conn, args):
    """Launch a named claude session in a kitty tab/pane and record its placeholder
    so the SessionStart hook adopts Claude's UUID onto it. A -t/--template preloads
    SpawnSpec defaults from ~/.herd/templates/<name>.toml; CLI flags override. See
    herd.spawn / herd.template."""
    herd_args, claude_args = _split_dashdash(args)
    p = argparse.ArgumentParser(prog="herd spawn", add_help=False)
    p.add_argument("job", nargs="?", default=None)   # optional: a template may supply it
    p.add_argument("-t", "--template", default=None)
    p.add_argument("--cwd", default=None)
    p.add_argument("--prompt", default=None)
    # --type tab|pane, with --tab / --pane as shorthand. Mutually exclusive so
    # `--tab --pane` (or --type with either) is a clean usage error. Default is
    # None (not "tab") so the resolver can tell "unset" from an explicit --tab and
    # let a template's `type` win when the flag is absent.
    t = p.add_mutually_exclusive_group()
    t.add_argument("--type", dest="launch_type", choices=("tab", "pane"), default=None)
    t.add_argument("--tab", dest="launch_type", action="store_const", const="tab")
    t.add_argument("--pane", dest="launch_type", action="store_const", const="pane")
    try:
        ns = p.parse_args(herd_args)
    except SystemExit:
        print("usage: herd spawn [<job>] [-t NAME] [--cwd DIR] "
              "[--tab|--pane|--type tab|pane] [--prompt TEXT] [-- <claude args...>]")
        return 2
    try:
        tmpl = load_template(ns.template) if ns.template else {}
        spec = resolve_spec({"job": ns.job, "cwd": ns.cwd, "launch_type": ns.launch_type,
                             "prompt": ns.prompt, "claude_args": claude_args}, tmpl)
    except ValueError as e:
        print("✗ " + str(e))
        return 1
    ok, msg, _ = spawn(conn, spec, os.environ.get("KITTY_LISTEN_ON"), _now_iso())
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_tcomplete(conn, args):
    """Template-name completion feed for `herd spawn -t` (machinery, hidden)."""
    print("\n".join(available_templates()))
    return 0


COMMANDS = {"ls": cmd_ls, "jump": cmd_jump, "spawn": cmd_spawn, "watch": cmd_watch,
            "preview": cmd_preview, "complete": cmd_complete, "tcomplete": cmd_tcomplete,
            "rows": cmd_rows, "poke": cmd_poke}
# `spawn` writes (W1) and `watch` focuses windows — neither is readonly.
_READONLY = {"ls", "preview", "complete", "tcomplete", "rows", "poke"}
# The verbs a user actually types. `preview` (fzf's per-highlight pane), `complete`
# / `tcomplete` (tab-completion feeds), `rows` (fzf's reload source) and `poke`
# (watch's refresh child) are machinery — callable, but not advertised in help or
# tab-completion.
USER_COMMANDS = ("ls", "jump", "spawn", "watch", "doctor")

# doctor opens nothing up front: a missing or corrupt DB is something it REPORTS,
# and the shared connect below would traceback on exactly the machines it exists
# to diagnose.
_NO_DB = {"doctor"}


def cmd_doctor(argv):
    from herd import doctor                       # local: only this verb needs it
    return doctor.main(argv)


# Verbs that take NO arguments at all. `spawn` parses its own (argparse, plus a
# `--` passthrough), `jump`/`preview` take one operand, `watch` takes one optional
# flag — those validate themselves. watch left this set when --one-shot landed, so
# `herd watch --typo` is refused by main()'s own watch guard instead (before the DB
# is opened), with a matching one in cmd_watch for direct callers.
_NO_ARGS = {"ls", "rows", "complete", "tcomplete", "poke"}

USAGE = """usage: herd <command> [args]

  ls              list live sessions (the default)
  jump [query]    focus a session; fuzzy-pick when the query is not unique
  spawn <job>     launch a named claude session in a kitty tab/pane
  watch           the picker as a permanent dashboard
    --one-shot    exit after one jump (for a kitty overlay panel)
  doctor          diagnose the install

  query = herd id, name, job, uuid prefix, or cwd substring"""


def main(argv=None):
    """An unrecognized flag is REFUSED, not repurposed — as in herd.install.main
    and herd.daemon.main. `herd ls --help` used to ignore the flag and list;
    `herd jump --foo` searched for a session named '--foo' and, finding none,
    opened the picker over everything. Neither is destructive, which is why this is
    the third of these rather than the first, but a flag the CLI cannot read still
    means the caller asked for something it is not doing.
    """
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--help", "-h", "help"):
        print(USAGE)
        return 0
    cmd = argv[0] if argv else "ls"
    if cmd in _NO_DB:
        return cmd_doctor(argv[1:])
    if cmd not in COMMANDS:
        print(f"herd: unknown command {cmd!r} (try: {', '.join(USER_COMMANDS)})")
        return 2
    rest = argv[1:]
    if cmd in _NO_ARGS and rest:
        print(f"herd {cmd}: takes no arguments (got {' '.join(repr(a) for a in rest)})")
        print()
        print(USAGE)
        return 2
    if cmd in ("jump", "preview") and rest and rest[0].startswith("-"):
        print(f"herd {cmd}: unknown option {rest[0]!r} — a query is not a flag")
        print()
        print(USAGE)
        return 2
    # watch is out of _NO_ARGS (it takes --one-shot), so the guard above no longer
    # covers it. It gets its own, HERE rather than in cmd_watch, because refusing
    # argv must not cost a DB open — test_cli_refuses_unknown_arguments pins that,
    # and it caught this exact mistake. cmd_watch keeps a matching guard for direct
    # callers; the two must stay in agreement.
    if cmd == "watch" and rest and rest != ["--one-shot"]:
        print(f"herd watch: unknown option {rest[0]!r} — the only option is --one-shot")
        print()
        print(USAGE)
        return 2
    conn = connect(DEFAULT_DB, readonly=cmd in _READONLY)
    return COMMANDS[cmd](conn, rest)


if __name__ == "__main__":
    sys.exit(main())
