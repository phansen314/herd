"""T — jump / focus (kitty/focus.py + cli.py): pid->window resolution, the focus
path (re-derive, ack, self-heal), and cli display/resolve/completion."""
import contextlib
import inspect
import pathlib
import re
import shlex
import sqlite3
import sys
import socket
import time

import pytest

from herd.kitty import focus
from herd.kitty.focus import window_for_pid, flatten_windows, focus_session
from herd import cli

from helpers import T0, T1, SOCK, short_tmp_dir, mk_session, mk_herd, mk_attention

_WINS = [{"id": 1, "foreground_processes": [{"pid": 111, "cmdline": ["bash"]}]},
         {"id": 42, "foreground_processes": [{"pid": 5000, "cmdline": ["/opt/claude"]},
                                             {"pid": 5001, "cmdline": ["node"]}]}]


def test_window_for_pid():
    assert window_for_pid(_WINS, 5000) == 42
    assert window_for_pid(_WINS, 5001) is None    # non-claude proc with matching pid
    assert window_for_pid(_WINS, 999) is None


def test_flatten_windows():
    assert flatten_windows('[{"tabs":[{"windows":[{"id":9}]}]}]') == [{"id": 9}]
    assert flatten_windows("not json") is None


def _focus_fixture(fresh, win_stored=7, pid=5000, attn=T0):
    c = fresh()
    pk = mk_session(c, session_id="jz", pid=pid, cwd="/code/app")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=win_stored, source="hook")
    if attn:
        mk_attention(c, pk, attention_at=attn)
    return c, pk


def test_focus_rederives_acks_selfheals(fresh):
    c, pk = _focus_fixture(fresh, win_stored=7)
    calls = []
    ok, msg = focus_session(
        c, pk, T1,
        list_fn=lambda s: [{"id": 42, "foreground_processes": [{"pid": 5000, "cmdline": ["claude"]}]}],
        focus_fn=lambda s, w: (calls.append((s, w)) or True))
    assert ok and calls == [(SOCK, 42)]
    assert c.execute("SELECT window_id FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0] == 42
    assert c.execute("SELECT ack_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == T1


def test_focus_falls_back_to_cached_window(fresh):
    c, pk = _focus_fixture(fresh, win_stored=7, attn=None)
    calls = []
    ok, _ = focus_session(c, pk, T1, list_fn=lambda s: [], focus_fn=lambda s, w: (calls.append(w) or True))
    assert ok and calls == [7]


def test_focus_errors_on_kitty_failure_and_no_placement(fresh):
    c, pk = _focus_fixture(fresh)
    okf, _ = focus_session(c, pk, T1, list_fn=lambda s: [], focus_fn=lambda s, w: False)
    c2 = fresh()
    p2 = mk_session(c2, session_id="np")   # no herd_sessions row
    okn, _ = focus_session(c2, p2, T1, list_fn=lambda s: [], focus_fn=lambda s, w: True)
    assert not okf and not okn


def test_resolve_matches_all_keys(fresh):
    c = fresh()
    a = mk_session(c, session_id="aaa11111", session_name="refactor-api", cwd="/x/api")
    mk_herd(c, a, job_name="api", kitty_socket=SOCK, window_id=1)
    b = mk_session(c, session_id="bbb22222", cwd="/y/web")
    ids = lambda ms: sorted(r["id"] for r in ms)
    assert ids(cli.resolve(c, str(a))) == [a]          # herd id
    assert ids(cli.resolve(c, "aaa1")) == [a]          # uuid prefix
    assert ids(cli.resolve(c, "refactor")) == [a]      # /rename name
    assert ids(cli.resolve(c, "web")) == [b]           # cwd substring
    assert ids(cli.resolve(c, "api")) == [a]           # exact job
    assert cli.resolve(c, "nomatch") == []


def test_resolve_refuses_empty_query(fresh):
    c = fresh()
    assert cli.resolve(c, "") == [] and cli.resolve(c, "   ") == []


_NAMED = {"id": 3, "session_id": "abc12345", "session_name": "my-refactor", "status": "waiting",
          "job_name": "api", "total_cost_usd": 1.5, "cwd": "/x/api", "attention_at": T0,
          "ack_at": None}


def test_row_line_shows_name_keeps_hidden_id():
    line = cli._row_line(_NAMED)
    assert line.split("\t", 1)[0] == "3" and "my-refactor" in line and "waiting" in line


def test_name_fallback_chain():
    assert cli._name(_NAMED) == "my-refactor"
    assert cli._name({"session_name": None, "job_name": "api", "session_id": "x"}) == "api"
    assert cli._name({"session_name": None, "job_name": None, "session_id": "abc12345-z"}) == "abc12345"


def test_parse_pick_maps_and_fails_safe():
    rows = [{"id": 3}, {"id": 9}]
    assert cli._parse_pick(rows, "3\t …")["id"] == 3
    assert cli._parse_pick(rows, "") is None
    assert cli._parse_pick(rows, "x\t") is None
    assert cli._parse_pick(rows, "99\t") is None


def test_preview_text_renders_detail():
    pv = cli._preview_text({"id": 3, "session_id": "abc12345", "session_name": "my-refactor",
                            "status": "waiting", "status_source": "hook", "cwd": "/x/api",
                            "total_cost_usd": 1.5, "context_percent": 42,
                            "attention_at": "2026-07-15T10:00:00.000Z"})
    # the attention line names the REASON now, not a generic "needs attention"
    for s in ("my-refactor", "abc12345", "waiting", "/x/api", "42%", "$1.50",
              "🙋 waiting for you since"):
        assert s in pv


def test_complete_tokens_dedup_sorted():
    toks = cli._complete_tokens([
        {"session_name": "refactor-api", "session_id": "aaa11111-x", "job_name": "api", "cwd": "/x/api/"},
        {"session_name": None, "session_id": "bbb22222-y", "job_name": None, "cwd": "/y/web"},
        {"session_name": None, "session_id": None, "job_name": None, "cwd": "/"}])
    assert toks == ["aaa11111", "api", "bbb22222", "refactor-api", "web"]


def _watch_fixture(fresh):
    c = fresh()
    pk = mk_session(c, session_id="w1", cwd="/x/api", status="working")
    mk_herd(c, pk, job_name="api", kitty_socket=SOCK, window_id=1)
    return c, pk


def test_rows_round_trip_through_parse_pick(fresh):
    """What `herd rows` emits must survive the trip fzf makes it take."""
    c, pk = _watch_fixture(fresh)
    rows = cli._live(c)
    text = cli._rows_text(c)
    assert cli._parse_pick(rows, text.splitlines()[0])["id"] == pk


def test_poke_never_reloads_while_rows_unchanged(fresh):
    """An unchanged list must not redraw the pane — only liveness GETs (data=None)."""
    c, _ = _watch_fixture(fresh)
    sent = []
    why = cli._poke_loop(c, "1234", lambda u, d: sent.append(d), lambda s: None, rounds=3)
    assert why == "done"
    assert sent == [None, None, None]               # pinged 3x, reloaded 0x


def test_poke_reloads_once_per_change(fresh):
    c, pk = _watch_fixture(fresh)
    sent = []

    def sleep(_):
        if not sent:                      # mutate between the 1st and 2nd poll
            c.execute("UPDATE sessions SET status='waiting' WHERE id=?", (pk,))

    why = cli._poke_loop(c, "1234", lambda u, d: sent.append(d), sleep, rounds=3)
    reloads = [d for d in sent if d is not None]
    assert why == "done" and len(reloads) == 1      # changed once -> reloaded once
    # Assert the EXACT path. `b"rows" in ...` also matched the old python command
    # (`-m herd.cli rows`) and would match the new one by coincidence of the
    # filename, so it could not tell the two apart — which is the whole change.
    assert reloads[0] == f"reload(cat {shlex.quote(cli._rows_file('1234'))})".encode()


def test_poke_survives_fzf_still_binding_then_reaps_on_a_quiet_herd(fresh):
    """Measured: watch spawns the poker before fzf binds, so early failures are
    startup, not death — exiting on them killed auto-refresh outright. Past the
    grace it must still reap itself, even with nothing changing."""
    c, _ = _watch_fixture(fresh)            # note: no mutation — a quiet dashboard
    tries = []

    def boom(u, d):
        tries.append(u)
        raise OSError("connection refused")

    assert cli._poke_loop(c, "1234", boom, lambda s: None, rounds=99) == "gone"
    assert len(tries) == cli._POKE_GRACE     # kept trying through the grace window


def test_poke_that_reached_fzf_once_dies_on_the_next_failure(fresh):
    """After first contact, a failure is a closed port — reap immediately."""
    c, _ = _watch_fixture(fresh)
    n = []

    def flaky(u, d):
        n.append(u)
        if len(n) > 1:                       # first call succeeds, then the port closes
            raise OSError("connection refused")

    assert cli._poke_loop(c, "1234", flaky, lambda s: None, rounds=99) == "gone"
    assert len(n) == 2                       # no grace once it has seen fzf alive


def test_watch_flags_listen_on_the_port_we_chose():
    flags = " ".join(cli._watch_flags(4321))
    assert "--listen=4321" in flags
    assert "ctrl-r:reload" in flags     # quit keys are --expect now, not a bind


def test_watch_does_not_rely_on_fzfs_start_event():
    """--listen=0 + `start:execute-silent` was measured to spawn the poker on one
    picker and not the next ($FZF_PORT unset when start fires). watch owns the port."""
    flags = " ".join(cli._watch_flags(cli._free_port()))
    assert "start:" not in flags and "--listen=0" not in flags


def test_free_port_is_usable_and_distinct():
    import socket
    a, b = cli._free_port(), cli._free_port()
    assert a != b
    with socket.socket() as s:
        s.bind(("127.0.0.1", a))            # actually bindable, i.e. really free


@pytest.mark.shell
def test_watch_and_jump_share_one_fzf_flag_list(monkeypatch, fresh):
    """watch must reuse _fzf_pick, or --delimiter/--with-nth drift breaks _parse_pick."""
    c, _ = _watch_fixture(fresh)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._fzf_pick(cli._live(c), "", cli._watch_flags(4321))
    assert "--delimiter" in seen["cmd"] and "--with-nth" in seen["cmd"]
    assert seen["cmd"].index("--listen=4321") > seen["cmd"].index("--with-nth")


def _watch_driver(monkeypatch, outs, delay=0.0):
    """Drive cmd_watch with canned fzf stdout, one per loop pass. Patches _fzf_run —
    NOT _fzf_pick: cmd_watch calls _fzf_run, and patching the wrong one lets the real
    fzf spawn and the loop spin forever (it did, and hung the suite)."""
    killed, seen = [], []
    monkeypatch.setattr(cli, "_has_fzf", lambda: True)

    class P:
        """Stands in for Popen, and must answer everything _reap_poker calls — a
        double that only had terminate() let a missing wait() pass here and fail in
        production, where the un-waited child stays defunct until the next Popen."""
        def terminate(self):
            killed.append(port_of[0])

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    port_of = [None]

    def spawn(port):
        port_of[0] = port
        return P()

    monkeypatch.setattr(cli, "_spawn_poker", spawn)

    def fake_run(rows, query, extra=()):
        seen.append(extra)
        if not outs:
            raise AssertionError("cmd_watch looped past its canned input")
        out = outs.pop(0)
        if isinstance(out, BaseException):
            raise out
        # A canned entry is either raw stdout or an explicit (stdout, rc) pair. The
        # bare-string form synthesises the rc fzf would really have returned — 130
        # for the empty output Esc produces, 0 for a selection — so the tests that
        # predate the exit code keep reading as what they were always describing.
        if delay:
            time.sleep(delay)               # a picker that hosted a real keypress
        if isinstance(out, tuple):
            return out
        return out, (130 if not out.strip() else 0)

    monkeypatch.setattr(cli, "_fzf_run", fake_run)
    return killed, seen


@pytest.mark.parametrize("key", ["ctrl-q", "ctrl-c"])
def test_watch_quits_on_either_quit_key(monkeypatch, fresh, key):
    """The regression that shipped: both keys used to read as Esc (fzf exits 130 with
    empty stdout for abort), so watch looped and the tab could not be left at all.
    ctrl-c cannot fall back on a signal — fzf's raw mode disables ISIG."""
    c, pk = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [f"{key}\n{pk}\t! #{pk}  api\n"])
    assert cli.cmd_watch(c, []) == 0        # returns, does not loop
    assert len(killed) == 1                 # and still reaps its poker


def test_watch_focuses_on_enter(monkeypatch, fresh):
    """Enter under --expect leaves the key line EMPTY, then the row."""
    c, pk = _watch_fixture(fresh)
    _watch_driver(monkeypatch, [f"\n{pk}\t! #{pk}  api\n", "ctrl-q\n"])
    focused = []
    monkeypatch.setattr(cli, "_do_focus", lambda conn, row: focused.append(row["id"]))
    assert cli.cmd_watch(c, []) == 0
    assert focused == [pk]                  # focused, then looped, then quit


def test_watch_reenters_the_picker_on_esc(monkeypatch, fresh):
    """Esc prints nothing at all — that must keep looping, not quit."""
    c, _ = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, ["", "", "ctrl-q\n"])
    assert cli.cmd_watch(c, []) == 0
    assert len(killed) == 3                 # three pickers, three pokers reaped


def test_watch_stops_instead_of_respawning_a_picker_that_cannot_start(monkeypatch, fresh):
    """fzf exits 2 when it could not run at all — an unknown --bind, a --listen port
    taken between _free_port and exec, a terminal too small for --height. That is
    indistinguishable from Esc by stdout alone (both empty), so the loop re-entered
    with no delay, binding a port and forking a poker every pass. Measured on the
    unfixed loop: 5000 iterations in 0.25s, each one a real socket bind and fork.

    The canned list holds ONE entry on purpose: a second pass would consume it, and
    _watch_driver raises when cmd_watch loops past its input. Looping is the bug."""
    c, _ = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [("", 2)])
    assert cli.cmd_watch(c, []) == 2
    assert len(killed) == 1                 # the one poker it did fork was reaped


def test_watch_gives_up_after_repeated_instant_empty_pickers(monkeypatch, fresh):
    """The backstop behind the exit code, for a picker that fails while exiting 0 or
    1. Nothing returned AND returned too fast to have been a keypress, N times
    running. The canned list is one longer than the limit: tripping at the limit
    means the last entry is never consumed."""
    c, _ = _watch_fixture(fresh)
    canned = [("", 1)] * (cli._PICKER_MAX_FAST + 1)
    killed, _ = _watch_driver(monkeypatch, canned)
    assert cli.cmd_watch(c, []) == 2
    assert len(killed) == cli._PICKER_MAX_FAST
    assert len(canned) == 1, "gave up one pass too late"


def test_a_slow_esc_never_trips_the_backstop(monkeypatch, fresh):
    """The counter must key on FAST empties, not empties. Esc is a legitimate empty
    result and the dashboard is a thing people sit in — dismissing the picker twenty
    times over an afternoon must not look like a failing fzf. Simulated by making
    each canned picker take longer than the threshold to return."""
    c, _ = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [""] * 8 + ["ctrl-q\n"],
                              delay=cli._PICKER_MIN_SECS * 1.5)
    assert cli.cmd_watch(c, []) == 0
    assert len(killed) == 9


def test_one_genuine_esc_clears_the_fast_empty_count(monkeypatch, fresh):
    """CONSECUTIVE, not cumulative. A real interaction between two instant empties
    has to reset the counter, or a long-lived dashboard eventually trips on noise it
    already recovered from."""
    c, _ = _watch_fixture(fresh)
    pk = cli._live(c)[0]["id"]
    monkeypatch.setattr(cli, "_do_focus", lambda conn, row: None)
    canned = ([("", 1)] * 4) + [f"\n{pk}\tpick\n"] + ([("", 1)] * 4) + ["ctrl-q\n"]
    _watch_driver(monkeypatch, canned)
    assert cli.cmd_watch(c, []) == 0


def test_watch_reaps_its_poker_even_when_the_picker_raises(monkeypatch, fresh):
    """One poker per picker. A pick, a cancel, or a crash must not leave one behind."""
    c, _ = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [KeyboardInterrupt()])
    assert cli.cmd_watch(c, []) == 130
    assert len(killed) == 1


# ── watch --one-shot: the kitty overlay mode ────────────────────────────────
# The tests above all drive cmd_watch(c, []) and pin the LOOPING default. That is
# deliberate and load-bearing: --one-shot must not change the dedicated-tab
# behavior, and test_watch_reenters_the_picker_on_esc above is the exact assertion
# --one-shot inverts. If both keep passing, the flag is genuinely opt-in.
#
# _watch_driver raises "cmd_watch looped past its canned input" when the loop asks
# for more fzf output than it was given, so handing it exactly ONE canned picker is
# how these tests prove watch exited rather than looped.


def test_one_shot_exits_after_a_jump(monkeypatch, fresh):
    """Enter focuses and returns — one picker, not two. This is the whole feature:
    the overlay dies with the process, so looping strands it on the origin window."""
    c, pk = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [f"\n{pk}\t! #{pk}  api\n"])
    focused = []
    monkeypatch.setattr(cli, "_do_focus", lambda conn, row: focused.append(row["id"]))
    assert cli.cmd_watch(c, ["--one-shot"]) == 0
    assert focused == [pk]
    assert len(killed) == 1                 # focused, exited, and still reaped


def test_one_shot_dismisses_on_esc(monkeypatch, fresh):
    """Esc prints nothing at all. Default watch re-enters the picker; a panel you
    cannot close with Esc is a bug, so one-shot exits 130 (cancel, as cmd_jump)."""
    c, _ = _watch_fixture(fresh)
    killed, _ = _watch_driver(monkeypatch, [""])
    assert cli.cmd_watch(c, ["--one-shot"]) == 130
    assert len(killed) == 1


@pytest.mark.parametrize("key", ["ctrl-q", "ctrl-c"])
def test_one_shot_quits_on_the_quit_keys_too(monkeypatch, fresh, key):
    """The quit keys are unchanged by the flag — the one path both modes share."""
    c, pk = _watch_fixture(fresh)
    _watch_driver(monkeypatch, [f"{key}\n{pk}\t! #{pk}  api\n"])
    assert cli.cmd_watch(c, ["--one-shot"]) == 0


def test_one_shot_returns_when_the_herd_is_empty(monkeypatch, fresh):
    """Default watch sleeps until a session shows up. An overlay doing that is a
    stuck panel with nothing in it, so one-shot returns 1 like `jump` on no rows."""
    c = fresh()                             # no sessions at all
    monkeypatch.setattr(cli, "_has_fzf", lambda: True)

    def no_sleep(_):
        raise AssertionError("one-shot must not wait for a session to appear")

    # cmd_watch does `import time` locally, so there is no cli.time to patch —
    # patch the stdlib module its local import resolves to.
    monkeypatch.setattr("time.sleep", no_sleep)
    assert cli.cmd_watch(c, ["--one-shot"]) == 1


def test_one_shot_keeps_the_panel_up_on_an_unresolvable_pick(monkeypatch, fresh):
    """A pick whose session died between the pick and the lookup must NOT exit:
    '✗ that session is gone' printed as the overlay tears down is unreadable. So
    the panel stays and the message can be read — hence a SECOND picker here."""
    c, _ = _watch_fixture(fresh)
    gone = "9999\t! #9999 vanished\n"
    killed, _ = _watch_driver(monkeypatch, [f"\n{gone}", "ctrl-q\n"])
    assert cli.cmd_watch(c, ["--one-shot"]) == 0    # looped, then quit
    assert len(killed) == 2                         # two pickers, two pokers


def test_one_shot_header_tells_the_truth_about_esc(fresh):
    """The header is the only flag that differs. Both modes keep --expect: one-shot
    changes what cmd_watch does with the result, not how fzf behaves."""
    plain, once = cli._watch_flags(4321), cli._watch_flags(4321, one_shot=True)
    assert "esc dismiss" in once[once.index("--header") + 1]
    assert "esc" not in plain[plain.index("--header") + 1]
    assert "--expect=ctrl-q,ctrl-c" in plain and "--expect=ctrl-q,ctrl-c" in once


def test_watch_refuses_unknown_options(monkeypatch, fresh):
    """watch left _NO_ARGS to take --one-shot, so main()'s generic no-args guard no
    longer covers it and cmd_watch is the ONLY thing refusing a typo. Without this
    test, `herd watch --one-shit` silently runs the forever-loop in an overlay.
    See main()'s docstring: an unrecognized flag is refused, never repurposed."""
    c, _ = _watch_fixture(fresh)

    def no_poker(port):
        raise AssertionError("refused argv must not reach the picker")

    monkeypatch.setattr(cli, "_spawn_poker", no_poker)
    monkeypatch.setattr(cli, "_has_fzf", lambda: True)
    for bad in (["--foo"], ["--one-shot", "extra"], ["one-shot"], ["--one-shot=1"]):
        assert cli.cmd_watch(c, bad) == 2, bad


def test_watch_is_not_in_no_args_but_still_a_user_command():
    """The pair that keeps --one-shot reachable: out of _NO_ARGS (or main() rejects
    the flag before cmd_watch sees it), still in USER_COMMANDS (or it vanishes from
    help and tab-completion)."""
    assert "watch" not in cli._NO_ARGS
    assert "watch" in cli.USER_COMMANDS
    assert "--one-shot" in cli.USAGE


def test_parse_expect_matches_measured_fzf_output():
    """Byte-for-byte what fzf 0.44.1 writes, captured by injecting keys via a pty."""
    assert cli._parse_expect("ctrl-q\n1\tAAA\n") == ("ctrl-q", "1\tAAA\n")
    assert cli._parse_expect("ctrl-c\n1\tAAA\n") == ("ctrl-c", "1\tAAA\n")
    assert cli._parse_expect("\n1\tAAA\n") == ("", "1\tAAA\n")      # plain enter
    assert cli._parse_expect("") == ("", "")                        # esc: no output


def test_only_watch_expects_keys(fresh):
    """jump must NOT carry --expect: _parse_pick would read the key line as a row id."""
    assert "--expect=ctrl-q,ctrl-c" in cli._watch_flags(4321)
    assert not any("ctrl-q:abort" in f for f in cli._watch_flags(4321))
    src = inspect.getsource(cli.cmd_jump)
    assert "--expect" not in src and "_parse_expect" not in src


MACHINERY = ("preview", "complete", "rows", "poke")


def test_cli_hides_machinery():
    """The property, not the literal: every user verb is offered by tab-completion
    and no machinery verb is. Pinning the exact tuple (and the exact compgen line)
    meant adding a verb failed here twice, in two places, for no defect."""
    from herd import install as inst
    completion = inst.COMPLETION_SRC.read_text()
    offered = re.search(r'_herd_offer "\$cur" "([a-z ]+)"', completion).group(1).split()
    assert set(offered) == set(cli.USER_COMMANDS)
    assert set(MACHINERY) <= set(cli.COMMANDS)          # callable ...
    assert not set(MACHINERY) & set(cli.USER_COMMANDS)  # ... but not advertised
    assert not set(MACHINERY) & set(offered)


def test_cli_reads_live_sessions_only_through_r1_list():
    """cli must not re-transcribe the live read — that is what let write paths rot.
    _live() IS R1_list; ls, the picker and the preview pane all go through it."""
    import inspect
    src = inspect.getsource(cli)
    assert 'load_statements()' in src and '_STMT["R1_list"]' in src
    assert "FROM sessions" not in src, "cli transcribed SQL instead of using writes.sql"


def test_preview_serves_every_field_it_renders(fresh):
    """R1_list must carry the preview's columns (status_source was missing once)."""
    c, pk = _watch_fixture(fresh)
    row = next(r for r in cli._live(c) if r["id"] == pk)
    for col in ("status_source", "model", "git_branch", "context_percent",
                "started_at", "last_event_at", "last_event_type", "attention_at"):
        assert col in row.keys(), f"R1_list lacks {col}"
    assert "#" in cli._preview_text(row)


def test_watch_machinery_is_readonly_except_watch():
    """watch focuses windows (a write, via W6c_ack); its helpers only read."""
    assert {"rows", "poke"} <= cli._READONLY
    assert "watch" not in cli._READONLY


# ── cmd_jump: the four branches, none of which had a test ────────────────────
def _jump_env(monkeypatch, fresh, *, has_fzf=True, pick=None):
    """A live session + stubbed fzf/focus, so cmd_jump's control flow is what is
    under test rather than kitty or the picker."""
    c = fresh()
    pk = mk_session(c, session_id="uuid-aaaa", cwd="/code/herd", status="waiting")
    mk_herd(c, pk, job_name="api", created_at=T0, window_id=7)
    focused = []
    monkeypatch.setattr(cli, "_has_fzf", lambda: has_fzf)
    monkeypatch.setattr(cli, "_fzf_pick", lambda rows, q: pick(rows, q) if pick else None)
    monkeypatch.setattr(cli, "_do_focus",
                        lambda conn, row: (focused.append(row["id"]), 0)[1])
    return c, pk, focused


def test_jump_with_no_live_sessions_returns_1(monkeypatch, fresh):
    c = fresh()
    monkeypatch.setattr(cli, "_has_fzf", lambda: True)
    assert cli.cmd_jump(c, []) == 1


def test_jump_unique_match_focuses_without_the_picker(monkeypatch, fresh):
    """The scriptable path: `herd jump api` must not open fzf."""
    opened = []
    c, pk, focused = _jump_env(monkeypatch, fresh,
                               pick=lambda rows, q: opened.append(q))
    assert cli.cmd_jump(c, ["api"]) == 0
    assert focused == [pk] and opened == []


def test_jump_no_match_seeds_the_picker_over_all_sessions(monkeypatch, fresh):
    """0 matches is not an error — it opens the picker seeded with the query, so a
    typo is recoverable rather than fatal."""
    seen = {}
    def pick(rows, q):
        seen["rows"], seen["q"] = rows, q
        return None
    c, pk, focused = _jump_env(monkeypatch, fresh, pick=pick)
    cli.cmd_jump(c, ["nope-no-such"])
    assert seen["q"] == "nope-no-such"
    assert [r["id"] for r in seen["rows"]] == [pk]      # seeded over ALL live rows


def test_jump_cancelled_picker_is_quiet_130(monkeypatch, fresh):
    """fzf's cancel convention; must not be reported as an error."""
    c, pk, focused = _jump_env(monkeypatch, fresh, pick=lambda rows, q: None)
    assert cli.cmd_jump(c, []) == 130
    assert focused == []


def test_jump_picked_row_is_focused(monkeypatch, fresh):
    c, pk, focused = _jump_env(monkeypatch, fresh, pick=lambda rows, q: rows[0])
    assert cli.cmd_jump(c, []) == 0
    assert focused == [pk]


def test_jump_without_fzf_prints_instead_of_focusing(monkeypatch, fresh, capsys):
    c, pk, focused = _jump_env(monkeypatch, fresh, has_fzf=False)
    assert cli.cmd_jump(c, []) == 0
    assert focused == []
    assert "api" in capsys.readouterr().out


def test_jump_without_fzf_reports_an_unmatched_query(monkeypatch, fresh, capsys):
    c, pk, focused = _jump_env(monkeypatch, fresh, has_fzf=False)
    cli.cmd_jump(c, ["nope-no-such"])
    out = capsys.readouterr().out
    assert "no live session matches" in out and "api" in out   # explains, then lists


# ── kitty IO must be bounded ────────────────────────────────────────────────
# `kitten @` against a stale unix socket (the kitty is gone, the socket file is
# not) BLOCKS. These sit on the interactive path, so an unbounded call hangs
# `herd jump` with no output — on exactly the stale placement the cache tolerates.
@contextlib.contextmanager
def _hanging_socket():
    """A real AF_UNIX socket that is listening but never answers — the precise
    shape of a stale kitty socket, not an approximation of it.

    Binds under short_tmp_dir(), NOT pytest's tmp_path: sun_path caps at 104 bytes
    on macOS and tmp_path there is far longer, so bind() raised "AF_UNIX path too
    long" before the test could assert anything about timeouts."""
    with short_tmp_dir() as d:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        p = str(d / "kitty-stale")
        s.bind(p); s.listen(1)
        try:
            yield f"unix:{p}", s
        finally:
            s.close()


def test_ls_against_a_dead_kitty_gives_up(monkeypatch):
    monkeypatch.setattr(focus, "KITTY_TIMEOUT", 1)      # keep the test quick
    with _hanging_socket() as (sock, _srv):
        t0 = time.monotonic()
        out = focus._ls(sock)
        elapsed = time.monotonic() - t0
    assert out == ""                                    # -> falls back to the cache
    assert elapsed < 10, f"_ls blocked for {elapsed:.1f}s"


def test_focus_against_a_dead_kitty_reports_failure(monkeypatch):
    monkeypatch.setattr(focus, "KITTY_TIMEOUT", 1)
    with _hanging_socket() as (sock, _srv):
        t0 = time.monotonic()
        ok = focus._focus(sock, 7)
        elapsed = time.monotonic() - t0
    assert ok is False                                  # -> "kitty focus failed"
    assert elapsed < 10, f"_focus blocked for {elapsed:.1f}s"


def test_jump_without_kitten_installed_is_a_message_not_a_traceback(tmp_path, monkeypatch):
    """kitten absent from PATH raised FileNotFoundError out of `herd jump`."""
    monkeypatch.setenv("PATH", str(tmp_path))          # no kitten anywhere
    assert focus._ls("unix:/tmp/nope") == ""
    assert focus._focus("unix:/tmp/nope", 7) is False


def _preview_arg(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **k: seen.update(argv=argv) or
                        __import__("types").SimpleNamespace(stdout="", returncode=0))
    cli._fzf_run([], "")
    return seen["argv"][seen["argv"].index("--preview") + 1]


@pytest.mark.shell
def test_fzf_command_strings_survive_a_script_path_with_a_space(monkeypatch, tmp_path):
    """fzf hands --preview and reload(...) to `sh -c`. Unquoted, a path under a
    directory with a space broke the preview pane and ctrl-r refresh. The path is
    now the bash script rather than a venv interpreter — same lexer, same hazard."""
    import shlex
    sh = tmp_path / "My Tools" / "preview.sh"
    sh.parent.mkdir()
    sh.write_text("#!/bin/bash\n")
    sh.chmod(0o755)
    monkeypatch.setattr(cli, "_PREVIEW_SH", sh)
    assert shlex.split(_preview_arg(monkeypatch))[0] == str(sh)


@pytest.mark.shell
def test_preview_falls_back_to_python_when_the_script_is_not_executable(monkeypatch, tmp_path):
    """A pip/zip install can drop the mode bit. The pane must degrade to the slow
    python verb, not render blank — and that path needs the same quoting."""
    import shlex
    sh = tmp_path / "My Tools" / "preview.sh"
    sh.parent.mkdir()
    sh.write_text("#!/bin/bash\n")
    sh.chmod(0o644)                                     # readable, NOT executable
    monkeypatch.setattr(cli, "_PREVIEW_SH", sh)
    monkeypatch.setattr(cli.sys, "executable", "/opt/My Tools/venv/bin/python3")
    parts = shlex.split(_preview_arg(monkeypatch))
    assert parts[0] == "/opt/My Tools/venv/bin/python3"
    assert parts[1:4] == ["-m", "herd.cli", "preview"]


# ── the poker hands fzf the text it already computed ─────────────────────────
def test_poke_writes_the_rows_it_already_has(fresh, monkeypatch, tmp_path):
    """_poke_loop computes the row text in-process to detect the change, then used
    to tell fzf to start a fresh interpreter and compute the identical text again —
    79ms a refresh against ~1ms for a cat. The file must hold exactly what a `rows`
    run would have produced, or the pane shows something the CLI never would."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    c, pk = _watch_fixture(fresh)
    sent = []

    def sleep(_):
        if not sent:
            c.execute("UPDATE sessions SET status='waiting' WHERE id=?", (pk,))

    seen = {}

    def send(u, d):
        sent.append(d)
        if d is not None:                       # capture while the file still exists
            seen["text"] = pathlib.Path(cli._rows_file("1234")).read_text()

    cli._poke_loop(c, "1234", send, sleep, rounds=3)
    assert seen["text"].rstrip("\n") == cli._rows_text(c)


def test_poke_reload_does_not_spawn_an_interpreter(fresh, monkeypatch, tmp_path):
    """The regression that matters: any reload naming sys.executable is the 79ms
    path coming back."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    c, pk = _watch_fixture(fresh)
    sent = []

    def sleep(_):
        if not sent:
            c.execute("UPDATE sessions SET status='waiting' WHERE id=?", (pk,))

    cli._poke_loop(c, "1234", lambda u, d: sent.append(d), sleep, rounds=3)
    payload = [d for d in sent if d is not None][0].decode()
    assert sys.executable not in payload and "herd.cli rows" not in payload
    assert payload.startswith("reload(cat ")


@pytest.mark.parametrize("why,drive", [
    ("done", lambda c, pk: None),
    ("db", lambda c, pk: c.close()),
])
def test_poke_removes_its_rows_file_on_every_exit(fresh, monkeypatch, tmp_path, why, drive):
    """A per-process file with no reaper accumulates — the herd-db-err.$$ lesson."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    c, pk = _watch_fixture(fresh)
    path = pathlib.Path(cli._rows_file("1234"))

    def sleep(_):
        c.execute("UPDATE sessions SET status='waiting' WHERE id=?", (pk,))
        drive(c, pk)

    assert cli._poke_loop(c, "1234", lambda u, d: None, sleep, rounds=2) == why
    assert not path.exists(), f"rows file leaked on {why!r} exit"


def test_poke_gone_exit_also_cleans_up(fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    c, _ = _watch_fixture(fresh)
    path = pathlib.Path(cli._rows_file("1234"))

    def boom(u, d):
        raise OSError("connection refused")

    assert cli._poke_loop(c, "1234", boom, lambda s: None, rounds=99) == "gone"
    assert not path.exists()


def test_ctrl_r_still_forces_a_fresh_read(fresh):
    """ctrl-r is the explicit "refresh NOW" path. Pointing it at the file would
    answer a request for freshness with whatever the poker last wrote, so it keeps
    the python command and its 79ms — user-initiated and once per keypress."""
    flags = cli._watch_flags(1234)
    rbind = next(f for f in flags if f.startswith("--bind=ctrl-r"))
    assert cli._ROWS_CMD in rbind and "cat " not in rbind


def test_rows_file_survives_a_runtime_dir_with_a_space(fresh, monkeypatch, tmp_path):
    """The runtime dir is user-controlled and the payload goes through `sh -c`."""
    spaced = tmp_path / "run time"
    spaced.mkdir()
    monkeypatch.setenv("HERD_RUNTIME", str(spaced))
    c, pk = _watch_fixture(fresh)
    sent = []

    def sleep(_):
        if not sent:
            c.execute("UPDATE sessions SET status='waiting' WHERE id=?", (pk,))

    cli._poke_loop(c, "1234", lambda u, d: sent.append(d), sleep, rounds=3)
    payload = [d for d in sent if d is not None][0].decode()
    inner = payload[len("reload("):-1]                 # strip reload( ... )
    assert shlex.split(inner) == ["cat", cli._rows_file("1234")]


# ── watch must resolve the pick against LIVE rows, not the seed ─────────────
def test_watch_focuses_a_session_that_appeared_after_the_picker_opened(monkeypatch, fresh):
    """watch's whole point is a dashboard left open, and the poker reloads the pane
    from the rows file while it is. Resolving the selection against the SEED list
    meant enter on a session that arrived since did nothing at all — no focus, no
    message, straight back to the picker. Auto-refresh made its own results
    unselectable."""
    c, pk = _watch_fixture(fresh)
    late = mk_session(c, session_id="w2", cwd="/x/late", status="working")
    mk_herd(c, late, job_name="late", kitty_socket=SOCK, window_id=2)

    focused = []
    monkeypatch.setattr(cli, "_do_focus", lambda conn, row: focused.append(row["id"]))
    seeded = []

    def fake_run(rows, query, extra=()):
        seeded.append([r["id"] for r in rows])
        # fzf returns the LATE session — on screen via reload, absent from the seed
        out = f"\n{late}\t! #{late}  late\n" if len(seeded) == 1 else "ctrl-q\n"
        return out, 0

    monkeypatch.setattr(cli, "_has_fzf", lambda: True)
    monkeypatch.setattr(cli, "_spawn_poker",
                        lambda port: type("P", (), {"terminate": lambda s: None,
                                                    "wait": lambda s, timeout=None: 0,
                                                    "kill": lambda s: None})())
    monkeypatch.setattr(cli, "_fzf_run", fake_run)
    monkeypatch.setattr(cli, "_live", _late_after_first_call(c, late))
    assert cli.cmd_watch(c, []) == 0
    assert focused == [late], "enter on a newly-arrived session must focus it"


def _late_after_first_call(conn, late_pk):
    """_live that hides the late session from the SEED and reveals it afterwards —
    the picker's list going stale, which is what the poker's reload causes."""
    calls = [0]
    real = cli._live

    def live(c):
        calls[0] += 1
        rows = real(c)
        return [r for r in rows if r["id"] != late_pk] if calls[0] == 1 else rows
    return live


def test_watch_leaves_no_rows_file_behind(monkeypatch, fresh, tmp_path):
    """The integration half of test_reaping_the_poker_removes_its_rows_file: that one
    calls _reap_poker directly, so it cannot fail against a cmd_watch that never
    calls it. This pins the loop itself — one picker, one port, nothing left over."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    c, pk = _watch_fixture(fresh)
    ports = []
    monkeypatch.setattr(cli, "_free_port", lambda: 45123)

    def spawn(port):
        ports.append(port)
        cli._write_rows(cli._rows_file(port), "seed")   # what the real poker does
        return type("P", (), {"terminate": lambda s: None,
                              "wait": lambda s, timeout=None: 0,
                              "kill": lambda s: None})()

    monkeypatch.setattr(cli, "_has_fzf", lambda: True)
    monkeypatch.setattr(cli, "_spawn_poker", spawn)
    monkeypatch.setattr(cli, "_fzf_run", lambda rows, q, extra=(): ("ctrl-q\n", 0))
    assert cli.cmd_watch(c, []) == 0
    assert ports == [45123]
    assert not list(tmp_path.glob("herd-rows-*")), "watch left its poker's rows file"


def test_watch_says_so_when_the_pick_cannot_be_resolved(monkeypatch, fresh, capsys):
    """A session that really did end between the pick and the lookup must not be a
    silent no-op either."""
    c, pk = _watch_fixture(fresh)
    _watch_driver(monkeypatch, [f"\n9999\t! #9999  gone\n", "ctrl-q\n"])
    assert cli.cmd_watch(c, []) == 0
    assert "gone" in capsys.readouterr().out


# ── the poker's rows file must not survive its SIGTERM ──────────────────────
def test_reaping_the_poker_removes_its_rows_file(tmp_path, monkeypatch):
    """_poke_loop's finally CANNOT run under terminate() — SIGTERM's default
    disposition kills the interpreter without unwinding, verified. So watch left one
    herd-rows-<port> per picker, each on a fresh random port, in the runtime dir."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    port = 45999
    cli._write_rows(cli._rows_file(port), "seed")
    assert pathlib.Path(cli._rows_file(port)).exists()

    class P:
        def terminate(self): self.term = True
        def wait(self, timeout=None): return 0
        def kill(self): pass

    cli._reap_poker(P(), port)
    assert not pathlib.Path(cli._rows_file(port)).exists()


def test_reap_poker_kills_a_poker_that_ignores_sigterm(tmp_path, monkeypatch):
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))
    events = []

    class Stubborn:
        def terminate(self): events.append("term")
        def wait(self, timeout=None):
            if len(events) < 2:
                raise __import__("subprocess").TimeoutExpired("poke", timeout)
            return 0
        def kill(self): events.append("kill")

    cli._reap_poker(Stubborn(), 46000)            # must not raise
    assert events == ["term", "kill"]


def test_reap_poker_never_raises_on_a_dead_poker(tmp_path, monkeypatch):
    """It runs in a finally — an exception here would replace watch's real outcome."""
    monkeypatch.setenv("HERD_RUNTIME", str(tmp_path))

    class Dead:
        def terminate(self): raise OSError("no such process")
        def wait(self, timeout=None): return 0
        def kill(self): pass

    cli._reap_poker(Dead(), 46001)


# ── FZF_PORT lands in a path, so it is validated ────────────────────────────
@pytest.mark.parametrize("bad", ["", "  ", "../../etc/x", "80; rm -rf /", "abc"])
def test_poke_refuses_a_port_that_is_not_a_number(bad, monkeypatch, fresh):
    """_rows_file interpolates the port into a filename, so a non-numeric value
    writes and unlinks outside the runtime dir."""
    monkeypatch.setenv("FZF_PORT", bad)
    monkeypatch.setattr(cli, "_poke_loop",
                        lambda *a, **k: pytest.fail("must not start a poke loop"))
    assert cli.cmd_poke(fresh(), []) == 1


# ── argv: a flag the CLI cannot read is refused, not repurposed ─────────────
@pytest.mark.parametrize("argv,why", [
    (["ls", "--help"], "ls ignored the flag and listed"),
    (["ls", "extra"], "ls takes no operand"),
    (["watch", "--foo"], "watch's only option is --one-shot"),
    (["watch", "--one-shot", "extra"], "--one-shot takes no operand"),
    (["jump", "--foo"], "searched for a session named '--foo'"),
    (["preview", "-1"], "not an id"),
])
def test_cli_refuses_unknown_arguments(argv, why, monkeypatch, capsys):
    monkeypatch.setattr(cli, "connect",
                        lambda *a, **k: pytest.fail(f"must not open the DB: {why}"))
    assert cli.main(argv) == 2, why
    assert "usage" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["ls", "jump", "watch", "spawn", "poke"])
def test_an_unopenable_db_is_a_message_not_a_traceback(cmd, monkeypatch, capsys):
    """The shared connect() is the FIRST thing every verb does, and the one that
    fails on a machine herd is not installed on yet — or one with a typo'd HERD_DB
    in ~/.herd/config. Unguarded it printed a raw sqlite3 traceback, which is the
    worst possible first contact with the tool.

    doctor got this treatment long ago (its whole job is broken machines) and the
    reasoning was never carried across to the five verbs a new user actually reaches
    first. The wording comes from daemon._fault_hint so it cannot drift from the
    daemon's advice for the same fault."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(cli, "connect", boom)
    assert cli.main([cmd]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "HERD_DB" in err and "herd.install" in err


def test_cli_lets_one_shot_through(monkeypatch):
    """The other half of the guard above: refusing typos is only correct if the one
    real flag still REACHES cmd_watch. A guard that rejects everything would pass
    every refusal test and silently break the overlay binding."""
    got = []
    monkeypatch.setattr(cli, "connect", lambda *a, **k: None)
    # setITEM, not setattr: COMMANDS captured the function object at import, so
    # patching cli.cmd_watch leaves dispatch pointing at the original.
    monkeypatch.setitem(cli.COMMANDS, "watch", lambda conn, args: got.append(args) or 0)
    assert cli.main(["watch", "--one-shot"]) == 0
    assert got == [["--one-shot"]]


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_cli_help_is_help(flag, monkeypatch, capsys):
    monkeypatch.setattr(cli, "connect", lambda *a, **k: pytest.fail("must not open the DB"))
    assert cli.main([flag]) == 0
    assert "herd <command>" in capsys.readouterr().out


def test_a_real_query_is_still_a_query(monkeypatch, fresh):
    """The guard is for LEADING dashes only — a query is otherwise arbitrary text."""
    c = fresh()
    seen = []
    monkeypatch.setattr(cli, "connect", lambda *a, **k: c)
    # the COMMANDS dict binds the function at import — patching cli.cmd_jump alone
    # leaves main() dispatching the real one
    monkeypatch.setitem(cli.COMMANDS, "jump", lambda conn, args: (seen.append(args), 0)[1])
    assert cli.main(["jump", "my-session"]) == 0
    assert seen == [["my-session"]]
