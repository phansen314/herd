"""O (59-63) — statusline.sh end to end: the sink + render + fingerprint cache +
Path C adoption, driven through the real bash script."""
import json
import os
import pathlib
import subprocess

import pytest

from helpers import HOOKS, T0, T1, SOCK, mk_session, mk_herd

SL_PAY = {"session_id": "s1", "model": {"id": "claude-opus-4-8"}, "session_name": "sess",
          "cwd": "/code/herd", "context_window": {"used_percentage": 42.7},
          "cost": {"total_cost_usd": 1.50},
          "rate_limits": {"five_hour": {"used_percentage": 73.5, "resets_at": 1784172774},
                          "seven_day": {"used_percentage": 12, "resets_at": 1784259174}}}


# The payload as Claude Code really sends it — every field W5 sinks. SL_PAY above
# is the minimal one; this is the one that catches an unbound column. A parsed-and-
# rendered-but-never-stored field (api_duration_ms shipped that way) is invisible to
# any fixture that omits it.
SL_FULL = {**SL_PAY,
           "version": "2.1.90",
           "output_style": {"name": "Explanatory"},
           "workspace": {"current_dir": "/code/herd", "project_dir": "/code/herd"},
           "worktree": {"original_cwd": "/code/herd-main"},
           "exceeds_200k_tokens": True,
           "context_window": {"used_percentage": 42.7, "context_window_size": 1000000,
                              "total_input_tokens": 15500, "total_output_tokens": 320},
           "cost": {"total_cost_usd": 1.50, "total_api_duration_ms": 2300,
                    "total_lines_added": 156, "total_lines_removed": 23}}


def _statusline(hook_env, payload, env=None):
    return hook_env.run("statusline.sh", payload, env)


def test_sinks_every_payload_field(hook_env):
    """Each field the payload carries reaches its column. api_duration_ms is the
    canary: it was parsed and rendered for months while the column stayed NULL."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, SL_FULL)
    r = c.execute("SELECT api_duration_ms, lines_added, lines_removed, "
                  "total_input_tokens, total_output_tokens, context_window_size, "
                  "exceeds_200k_tokens, claude_code_version, output_style, "
                  "original_cwd FROM sessions WHERE session_id='s1'").fetchone()
    assert r["api_duration_ms"] == 2300
    assert (r["lines_added"], r["lines_removed"]) == (156, 23)
    assert (r["total_input_tokens"], r["total_output_tokens"]) == (15500, 320)
    assert r["context_window_size"] == 1000000
    assert r["claude_code_version"] == "2.1.90"
    assert r["output_style"] == "Explanatory"
    assert r["original_cwd"] == "/code/herd-main"
    # coerced to 0/1 in the hook's jq — a bare bool would land as the TEXT "true"
    assert r["exceeds_200k_tokens"] == 1


def test_exceeds_200k_false_is_zero_not_empty(hook_env):
    """`if . then 1 else 0 end` must emit 0 for false AND for an absent key —
    `// ""` would turn a legitimate false into NULL."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, {**SL_FULL, "exceeds_200k_tokens": False})
    assert c.execute("SELECT exceeds_200k_tokens FROM sessions "
                     "WHERE session_id='s1'").fetchone()[0] == 0


def test_git_worktree_lands_from_payload(hook_env):
    """workspace.git_worktree is the linked-worktree NAME, absent in a main tree."""
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, {**SL_FULL,
                           "workspace": {"current_dir": "/code/herd",
                                         "git_worktree": "feature-xyz"}})
    assert c.execute("SELECT git_worktree FROM sessions "
                     "WHERE session_id='s1'").fetchone()[0] == "feature-xyz"


def test_main_worktree_leaves_git_worktree_null(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, SL_FULL)          # workspace has no git_worktree key
    assert c.execute("SELECT git_worktree FROM sessions "
                     "WHERE session_id='s1'").fetchone()[0] is None


def test_real_git_worktree_end_to_end(hook_env, tmp_path):
    """The one path with no production evidence: no session has yet run inside a
    linked worktree. Build a real one, confirm the branch walk resolves through
    the `.git`-as-a-file indirection that a worktree checkout uses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-C", str(repo)]
    subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(g + ["config", "user.email", "t@t"], check=True)
    subprocess.run(g + ["config", "user.name", "t"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(g + ["add", "f"], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(g + ["worktree", "add", "-q", "-b", "feature-xyz", str(wt)], check=True)
    assert (wt / ".git").is_file()          # the indirection this test exists for

    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd=str(wt))
    _statusline(hook_env, {**SL_FULL, "cwd": str(wt),
                           "workspace": {"current_dir": str(wt),
                                         "git_worktree": "feature-xyz"}})
    r = c.execute("SELECT git_branch, git_worktree FROM sessions "
                  "WHERE session_id='s1'").fetchone()
    assert r["git_worktree"] == "feature-xyz"
    assert r["git_branch"] == "feature-xyz"   # resolved via `gitdir:` -> worktree HEAD


def test_sinks_metrics_and_renders_claude_name(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s1", cwd="/code/herd")
    mk_herd(c, pk, job_name="api-refactor", created_at=T0, window_id=5)
    r = _statusline(hook_env, SL_PAY)
    row = c.execute("SELECT context_percent,total_cost_usd,rate_limit_5h_percent,"
                    "rate_limit_5h_resets_at FROM sessions WHERE session_id='s1'").fetchone()
    assert row["context_percent"] == 42 and isinstance(row["context_percent"], int)
    assert row["total_cost_usd"] == 1.5
    assert row["rate_limit_5h_percent"] == 73.5 and row["rate_limit_5h_resets_at"] == "2026-07-16T03:32:54Z"
    # ⬢ shows Claude's session_name, NOT the tier-2 job_name.
    assert "⬢ sess" in r.stdout and "api-refactor" not in r.stdout


def test_identical_tick_is_fingerprint_hit(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    _statusline(hook_env, SL_PAY)                               # tick 1: sink
    before = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    r2 = _statusline(hook_env, SL_PAY)                          # tick 2: cache hit
    after = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    assert before == after and r2.stdout.strip() != ""         # no write, still renders


def test_path_c_adopts_reconciled_session(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, pid=4242, cwd="/code/herd", status="unknown", status_source="reconcile")
    mk_herd(c, pk, created_at=T0, window_id=5, source="hook")
    _statusline(hook_env, {"session_id": "uuid-x", "model": {"id": "opus"}, "cwd": "/code/herd",
                           "context_window": {"used_percentage": 30}, "cost": {"total_cost_usd": 0.10}},
                {"KITTY_WINDOW_ID": "5", "KITTY_LISTEN_ON": SOCK})
    row = c.execute("SELECT session_id,context_percent FROM sessions WHERE id=?", (pk,)).fetchone()
    assert row["session_id"] == "uuid-x" and row["context_percent"] == 30


def test_tick_on_stopped_session_is_noop(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="dead", cwd="/x", status="stopped", stopped_at=T1)
    _statusline(hook_env, {"session_id": "dead", "model": {"id": "opus"}, "cwd": "/x",
                           "context_window": {"used_percentage": 99}, "cost": {"total_cost_usd": 5}})
    row = c.execute("SELECT context_percent,stopped_at FROM sessions WHERE session_id='dead'").fetchone()
    assert row["context_percent"] is None and row["stopped_at"] == T1


def test_never_moves_last_event_at(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd", last_event_at=T0, last_event_type="tool")
    _statusline(hook_env, SL_PAY)
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] == T0


# ── DB failure must not be read as "row not adopted" ────────────────────────
def _errlog(hook_env):
    p = pathlib.Path(hook_env.runtime) / "err.log"
    return p.read_text().splitlines() if p.exists() else []


def test_a_failing_db_is_not_retried_as_an_adoption_miss(hook_env):
    """run prints "0" when W5 matched no row but "" when it FAILED. Treating the
    failure as a miss ran an adopt + a retry, and with the 3s busy_timeout that was
    ~9s of stall per render — on a hook that fires ~1/sec per session, precisely
    when the DB is already contended.

    Asserted as the DB-error COUNT rather than wall-clock: a corrupt DB fails
    instantly through the same branch a locked one reaches slowly, so this stays
    deterministic and costs no runtime. One attempt, not three."""
    pathlib.Path(hook_env.path).write_bytes(os.urandom(4096))     # not a database
    r = _statusline(hook_env, SL_PAY, env={"KITTY_WINDOW_ID": "3",
                                           "KITTY_LISTEN_ON": "unix:/tmp/kitty-1"})
    assert r.returncode == 0 and r.stdout.strip()          # still renders, still exits 0
    assert len(_errlog(hook_env)) == 1                     # W5 only — no adopt, no retry


def test_a_genuine_adoption_miss_still_adopts(hook_env):
    """The other side of the gate: a HEALTHY db reporting 0 changes must still take
    path C, or a reconciled-but-unadopted row never gets claimed."""
    c = hook_env.conn()
    pk = mk_session(c, cwd="/x")                            # no session_id yet
    mk_herd(c, pk, job_name="api", kitty_socket=SOCK, window_id=3)
    _statusline(hook_env, SL_PAY, env={"KITTY_WINDOW_ID": "3", "KITTY_LISTEN_ON": SOCK})
    row = c.execute("SELECT session_id, context_percent FROM sessions WHERE id=?", (pk,)).fetchone()
    assert row["session_id"] == SL_PAY["session_id"]        # adopted
    assert row["context_percent"] is not None              # and the retry wrote metrics
    assert _errlog(hook_env) == []


def test_a_relative_gitdir_resolves_against_the_repo_not_the_hook_cwd(hook_env, tmp_path):
    """Git writes RELATIVE gitdirs for submodules and worktrees
    ("gitdir: ../../.git/modules/foo"). Resolved against the hook's cwd they either
    miss — dropping the branch — or hit and record a DIFFERENT repo's HEAD into
    sessions.git_branch, which is wrong data rather than missing data."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "HEAD").write_text("ref: refs/heads/correct-branch\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../real\n")

    c = hook_env.conn()
    mk_session(c, session_id="g1", cwd=str(sub))
    pay = {**SL_PAY, "session_id": "g1", "cwd": str(sub)}
    r = subprocess.run(["bash", str(HOOKS / "statusline.sh")], input=json.dumps(pay),
                       capture_output=True, text=True, cwd="/",   # NOT inside the repo
                       env=dict(os.environ, HERD_DB=hook_env.path,
                                HERD_RUNTIME=hook_env.runtime,
                                HERD_ERRLOG=f"{hook_env.runtime}/err.log"))
    assert "correct-branch" in r.stdout
    assert c.execute("SELECT git_branch FROM sessions WHERE session_id='g1'"
                     ).fetchone()[0] == "correct-branch"


def test_session_end_removes_both_per_session_runtime_files(hook_env):
    """One leak per session otherwise — bounded on a tmpfs XDG_RUNTIME_DIR,
    unbounded under the /tmp fallback."""
    c = hook_env.conn()
    mk_session(c, session_id="e1", cwd="/x")
    rt = pathlib.Path(hook_env.runtime)
    (rt / "herd-tool-e1").write_text("1\n")
    (rt / "herd-stline-e1").write_text("fp\nL1\nL2\n")
    hook_env.run("session_end.sh", {"session_id": "e1"})
    assert not (rt / "herd-tool-e1").exists()
    assert not (rt / "herd-stline-e1").exists(), "the statusline cache file leaked"


# ── reset stamps: jq strflocaltime replaced two `date` forks per tick ─────────
@pytest.mark.parametrize("epoch", [
    1784172774, 1784259174,
    1767225600,   # midnight — the hour that pads to 12, not 0
    1765000000,   # a PM time
    1735689600,   # Jan 1: single-digit month AND day, both needing a zero stripped
    1730000000,
])
def test_reset_stamps_match_what_date_produced(hook_env, epoch):
    """The two `date -d @epoch` forks are gone; jq formats these in the parse it
    already runs. GNU date is the reference the old code used, so the new output
    must be indistinguishable from it — including the %-I/%-m/%-d zero-stripping,
    which jq cannot ask strftime for portably and does with sub() instead."""
    import subprocess
    pay = {**SL_PAY, "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": epoch},
                                     "seven_day": {"used_percentage": 10, "resets_at": epoch}}}
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    out = _statusline(hook_env, pay).stdout
    want5 = subprocess.run(["date", "-d", f"@{epoch}", "+%-I:%M%p"],
                           capture_output=True, text=True).stdout.strip()
    want7 = subprocess.run(["date", "-d", f"@{epoch}", "+%-m/%-d %-I:%M%p"],
                           capture_output=True, text=True).stdout.strip()
    assert f"5h 50% resets {want5}" in out, out
    assert f"7d 10% resets {want7}" in out, out


def test_reset_segment_is_omitted_when_the_payload_has_no_resets_at(hook_env):
    """A missing resets_at must drop the 'resets ...' suffix, not render an empty
    one or the literal format string."""
    pay = {**SL_PAY, "rate_limits": {"five_hour": {"used_percentage": 50},
                                     "seven_day": {"used_percentage": 10}}}
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    out = _statusline(hook_env, pay).stdout
    assert "5h 50%" in out and "7d 10%" in out
    assert "resets" not in out, out
    assert "%I" not in out and "%-I" not in out, out


@pytest.mark.parametrize("resets", ['"1784000000"', '"garbage"', "true", "[1,2]", "{}"],
                         ids=["numeric-string", "text", "bool", "array", "object"])
def test_a_bad_resets_at_type_loses_only_its_own_segment(hook_env, resets):
    """strflocaltime RAISES on a non-number, and a raise aborts the WHOLE jq
    filter — so one unexpected field type emptied all 23 outputs, rendered a bare
    `🧠 0%` and sank NOTHING to the DB. The `date -d` forks this replaced lost only
    their own segment; moving the formatting into jq must not couple one optional
    nested field to every other field."""
    pay = json.loads(
        '{"session_id":"s1","model":{"id":"claude-opus-4-8"},"session_name":"n",'
        '"cwd":"/code/herd","context_window":{"used_percentage":42},'
        '"cost":{"total_cost_usd":1.5},'
        '"rate_limits":{"five_hour":{"used_percentage":50,"resets_at":' + resets + '},'
        '"seven_day":{"used_percentage":10}}}')
    c = hook_env.conn()
    mk_session(c, session_id="s1", cwd="/code/herd")
    out = _statusline(hook_env, pay).stdout
    # everything that does not depend on resets_at still renders...
    assert "⬢ n" in out and "🧠 42%" in out and "$1.50" in out and "5h 50%" in out
    assert "resets" not in out                      # ...and only that segment is gone
    # ...and the DB sink still ran, which is the half no one would notice was missing
    row = c.execute("SELECT context_percent,total_cost_usd FROM sessions "
                    "WHERE session_id='s1'").fetchone()
    assert row["context_percent"] == 42 and row["total_cost_usd"] == 1.5
