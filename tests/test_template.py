"""herd spawn templates — TOML loading, and the CLI > template > default merge."""
import pytest

from herd import cli
from herd.spawn import resolve_spec
from herd.template import load_template, available_templates

from helpers import SOCK

REVIEW = '''\
cwd   = "~/code/herd"
type  = "pane"
job   = "review"
title = "review-tab"
args  = ["--model", "opus"]
prompt = """
Review the current diff.
Focus on the parser.
"""

[vars]
HERD_CONTEXT = "review"
'''


@pytest.fixture
def templates(tmp_path, monkeypatch):
    """A temp templates dir wired via HERD_TEMPLATES; returns a writer."""
    monkeypatch.setenv("HERD_TEMPLATES", str(tmp_path))

    def write(name, text):
        (tmp_path / f"{name}.toml").write_text(text)
    return write


# ── load_template ────────────────────────────────────────────────────────────
def test_load_maps_keys_and_keeps_multiline_prompt(templates):
    templates("review", REVIEW)
    t = load_template("review")
    assert t["cwd"] == "~/code/herd" and t["launch_type"] == "pane"
    assert t["job"] == "review" and t["title"] == "review-tab"
    assert t["claude_args"] == ["--model", "opus"]        # args -> claude_args
    assert t["vars"] == {"HERD_CONTEXT": "review"}
    assert t["prompt"] == "Review the current diff.\nFocus on the parser.\n"   # multiline preserved


def test_available_templates_lists_basenames(templates):
    templates("review", REVIEW)
    templates("web", 'cwd = "~/w"\n')
    assert available_templates() == ["review", "web"]


def test_unknown_key_is_rejected(templates):
    templates("bad", 'cwd = "/x"\nnope = 1\n')
    with pytest.raises(ValueError, match="unknown key 'nope'"):
        load_template("bad")


def test_bad_type_value_rejected(templates):
    templates("bad", 'type = "window"\n')
    with pytest.raises(ValueError, match="type must be"):
        load_template("bad")


def test_missing_file_and_bad_name(templates):
    with pytest.raises(ValueError, match="no template"):
        load_template("ghost")
    with pytest.raises(ValueError, match="invalid template name"):
        load_template("../etc/passwd")


def test_bad_toml_rejected(templates):
    templates("broken", 'cwd = "/x"\ntype =\n')
    with pytest.raises(ValueError, match="bad TOML"):
        load_template("broken")


# ── resolve_spec: CLI > template > default ───────────────────────────────────
def _cli(**kw):
    base = {"job": None, "cwd": None, "launch_type": None, "prompt": None, "claude_args": []}
    base.update(kw)
    return base


def test_template_only_fills_the_spec():
    t = {"job": "review", "cwd": "/code/herd", "launch_type": "pane",
         "prompt": "p", "title": "rt", "claude_args": ["--model", "opus"],
         "vars": {"K": "v"}}
    s = resolve_spec(_cli(), t)
    assert (s.job, s.cwd, s.launch_type, s.prompt, s.title) == \
        ("review", "/code/herd", "pane", "p", "rt")
    assert s.claude_args == ["--model", "opus"] and s.vars == {"K": "v"}


def test_cli_overrides_template():
    t = {"job": "review", "launch_type": "pane", "prompt": "tp"}
    s = resolve_spec(_cli(job="hotfix", launch_type="tab", prompt="cp"), t)
    assert (s.job, s.launch_type, s.prompt) == ("hotfix", "tab", "cp")


def test_launch_type_sentinel_lets_template_win():
    # flag unset (None) -> template's pane wins; default only when neither present.
    assert resolve_spec(_cli(job="j"), {"launch_type": "pane"}).launch_type == "pane"
    assert resolve_spec(_cli(job="j"), {}).launch_type == "tab"


def test_claude_args_append_template_then_cli():
    t = {"job": "j", "claude_args": ["--model", "opus"]}
    s = resolve_spec(_cli(claude_args=["--resume"]), t)
    assert s.claude_args == ["--model", "opus", "--resume"]


def test_job_from_template_or_cli_else_error():
    assert resolve_spec(_cli(), {"job": "review"}).job == "review"
    assert resolve_spec(_cli(job="cli"), {"job": "review"}).job == "cli"
    with pytest.raises(ValueError, match="job name is required"):
        resolve_spec(_cli(), {})


# ── cmd_spawn integration (spawn stubbed to capture the resolved spec) ────────
def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "spawn",
                        lambda conn, spec, socket, now, **kw: (seen.update(spec=spec, socket=socket)
                                                               or (True, "ok", 1)))
    monkeypatch.setenv("KITTY_LISTEN_ON", SOCK)
    return seen


def test_cmd_spawn_uses_template(templates, monkeypatch, fresh):
    templates("review", REVIEW)
    seen = _capture(monkeypatch)
    assert cli.cmd_spawn(fresh(), ["-t", "review"]) == 0
    s = seen["spec"]
    assert s.job == "review" and s.launch_type == "pane"
    assert s.prompt.startswith("Review the current diff.")
    assert s.claude_args == ["--model", "opus"] and s.vars == {"HERD_CONTEXT": "review"}


def test_cmd_spawn_cli_overrides_template(templates, monkeypatch, fresh):
    templates("review", REVIEW)
    seen = _capture(monkeypatch)
    assert cli.cmd_spawn(fresh(), ["hotfix", "-t", "review", "--tab", "--", "--resume"]) == 0
    s = seen["spec"]
    assert s.job == "hotfix" and s.launch_type == "tab"
    assert s.claude_args == ["--model", "opus", "--resume"]   # template + CLI append


def test_cmd_spawn_no_job_anywhere_errors(monkeypatch, fresh):
    _capture(monkeypatch)
    assert cli.cmd_spawn(fresh(), []) == 1        # no positional, no template job
