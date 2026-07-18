"""T — jump / focus (kitty/focus.py + cli.py): pid->window resolution, the focus
path (re-derive, ack, self-heal), and cli display/resolve/completion."""
import pytest

from herd.kitty.focus import window_for_pid, flatten_windows, focus_session
from herd import cli

from helpers import T0, T1, SOCK, mk_session, mk_herd, mk_attention

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


_NAMED = {"id": 3, "session_id": "abc12345", "session_name": "my-refactor",
          "status": "waiting", "job_name": "api", "total_cost_usd": 1.5, "cwd": "/x/api", "attn": 1}


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
    for s in ("my-refactor", "abc12345", "waiting", "/x/api", "42%", "$1.50", "needs attention"):
        assert s in pv


def test_complete_tokens_dedup_sorted():
    toks = cli._complete_tokens([
        {"session_name": "refactor-api", "session_id": "aaa11111-x", "job_name": "api", "cwd": "/x/api/"},
        {"session_name": None, "session_id": "bbb22222-y", "job_name": None, "cwd": "/y/web"},
        {"session_name": None, "session_id": None, "job_name": None, "cwd": "/"}])
    assert toks == ["aaa11111", "api", "bbb22222", "refactor-api", "web"]


def test_cli_hides_machinery():
    from herd import install as inst
    completion = inst.COMPLETION_SRC.read_text()
    assert cli.USER_COMMANDS == ("ls", "jump")
    assert {"preview", "complete"} <= set(cli.COMMANDS)
    assert "preview" not in cli.USER_COMMANDS
    assert 'compgen -W "ls jump"' in completion and "preview" not in completion
