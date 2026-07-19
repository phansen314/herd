"""hooks/preview.sh must render EXACTLY what cli._preview_text() renders.

herd carries two implementations of the preview formatter on purpose: the bash one
because fzf re-runs --preview on every highlight (78ms of python startup per
arrow-key was the whole reason), the python one because it is the fallback when the
script loses its +x and the reference this file pins against. Duplication without a
pin is the drift class this codebase spends the most effort preventing, so every row
shape below is asserted byte-for-byte. Change one formatter, change both.

Mirrors tests/test_hooks.py::test_bash_and_python_extract_same in intent — that
pins bash/python SQL EXTRACTION, this pins bash/python RENDERING.
"""
import pytest

from herd import cli
from herd.db import connect
from helpers import mk_attention, mk_herd, mk_session, T0, T1


def _full(c):
    """Every optional field populated, attention armed and unacked."""
    pk = mk_session(c, session_id="abcdef1234567890", pid=4242, cwd="/home/u/code/proj",
                    status="waiting", status_source="hook", session_name="refactor api",
                    last_event_at=T1, last_event_type="tool")
    c.execute("UPDATE sessions SET model='claude-opus-4-8', context_percent=42, "
              "total_cost_usd=1.5, git_branch='main' WHERE id=?", (pk,))
    mk_herd(c, pk, job_name="api")
    mk_attention(c, pk, attention_at=T1)
    return pk


def _bare(c):
    """Nothing optional set — every g() falls through to the em dash. Also the
    \\x1f\\x1f empty-field parse: tab would have collapsed these."""
    return mk_session(c, session_id="deadbeefcafe", cwd="/x", status="working")


def _zero_cost(c):
    """0.0 renders as $0.00, NOT the em dash — python tests `is not None`, so a
    falsy-vs-None slip in awk shows up here and nowhere else. Same for context 0%."""
    pk = mk_session(c, session_id="zero1", cwd="/z", status="working")
    c.execute("UPDATE sessions SET total_cost_usd=0.0, context_percent=0 WHERE id=?", (pk,))
    return pk


def _no_status_source(c):
    """status_source NULL -> the '  (hook)' suffix must vanish entirely."""
    return mk_session(c, session_id="nosrc1", cwd="/n", status="working")


def _name_job(c):
    """Name ladder rung 2: no session_name, falls back to the herd job name."""
    pk = mk_session(c, session_id="jobonly1", cwd="/j", status="working")
    mk_herd(c, pk, job_name="nightly")
    return pk


def _name_uuid(c):
    """Rung 3: no session_name, no job -> first 8 chars of the session id."""
    return mk_session(c, session_id="0123456789abcdef", cwd="/u", status="working")


def _name_dash(c):
    """Rung 4: nothing to name it by at all."""
    return mk_session(c, session_id=None, cwd="/d", status="working")


def _nasty_text(c):
    """A newline inside session_name and a space inside cwd, plus non-ASCII.

    THE reason rows are \\x1e-separated rather than newline-separated: under
    newline rows this value shifts every later row's fields and the pane renders a
    DIFFERENT session's data — a wrong preview, worse than a blank one.
    """
    return mk_session(c, session_id="nl1", cwd="/tmp/we ird", status="working",
                      session_name="line1\nline2 ünïcode \U0001f389")


def _armed_waiting(c):
    pk = mk_session(c, session_id="w1", cwd="/w", status="waiting")
    mk_attention(c, pk, attention_at=T1)
    return pk


def _armed_approval(c):
    pk = mk_session(c, session_id="a1", cwd="/a", status="needs_approval")
    mk_attention(c, pk, attention_at=T1)
    return pk


def _armed_working(c):
    pk = mk_session(c, session_id="s1", cwd="/s", status="working")
    mk_attention(c, pk, attention_at=T1)
    return pk


def _armed_unknown_status(c):
    """The MARK_UNKNOWN branch. 'unknown' — not an invented status: sessions.status
    carries a CHECK constraint, so the only statuses that can reach an armed row
    without a glyph are 'unknown' and 'stopped'."""
    pk = mk_session(c, session_id="u1", cwd="/u2", status="unknown")
    mk_attention(c, pk, attention_at=T1)
    return pk


def _armed_but_acked(c):
    """Armed AND acked -> the attention line is suppressed on both sides."""
    pk = mk_session(c, session_id="k1", cwd="/k", status="waiting")
    mk_attention(c, pk, attention_at=T1, ack_at=T1)
    return pk


SEEDS = [_full, _bare, _zero_cost, _no_status_source, _name_job, _name_uuid,
         _name_dash, _nasty_text, _armed_waiting, _armed_approval, _armed_working,
         _armed_unknown_status, _armed_but_acked]


@pytest.mark.parametrize("seed", SEEDS, ids=[s.__name__.lstrip("_") for s in SEEDS])
def test_bash_preview_matches_python(hook_env, seed):
    c = hook_env.conn()
    pk = seed(c)
    r = hook_env.run("preview.sh", None, args=[str(pk)])
    conn = connect(hook_env.path, readonly=True)
    row = next(x for x in cli._live(conn) if x["id"] == pk)
    assert r.returncode == 0
    assert r.stdout == cli._preview_text(row) + "\n"      # print() adds the newline


def test_dead_session_reads_gone(hook_env):
    """The cli.py:417 guarantee: preview reads live, so a session that died while
    the picker was open says so rather than rendering a stale row."""
    c = hook_env.conn()
    pk = mk_session(c, session_id="gone1", cwd="/g", status="waiting", stopped_at=T1)
    r = hook_env.run("preview.sh", None, args=[str(pk)])
    assert r.stdout == "(session gone)\n"
    assert r.returncode == 0


def test_unknown_id_reads_gone(hook_env):
    r = hook_env.run("preview.sh", None, args=["4242"])
    assert r.stdout == "(session gone)\n"
    assert r.returncode == 0


@pytest.mark.parametrize("args", [["abc"], ["1x"], [""], []])
def test_non_numeric_id_is_a_caller_bug(hook_env, args):
    """Mirrors cmd_preview's guard: rc=1 and no output. A bad id is the caller
    getting it wrong, which is not the same thing as a session having died."""
    r = hook_env.run("preview.sh", None, args=args)
    assert r.returncode == 1
    assert r.stdout == ""


def test_preview_survives_a_session_with_no_herd_row(hook_env):
    """R1_list LEFT JOINs herd_sessions; a tier-1-only session (no spawn, no job)
    must still render — the NULL job/socket/window columns are the common case."""
    c = hook_env.conn()
    pk = mk_session(c, session_id="tier1only", cwd="/t", status="working",
                    started_at=T0, last_event_at=T0, last_event_type="tool")
    r = hook_env.run("preview.sh", None, args=[str(pk)])
    conn = connect(hook_env.path, readonly=True)
    row = next(x for x in cli._live(conn) if x["id"] == pk)
    assert r.stdout == cli._preview_text(row) + "\n"
