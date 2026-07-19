"""herd spawn templates — TOML loading, and the CLI > template > default merge."""
import importlib.util
import sys

import pytest

from herd import cli
from herd.spawn import resolve_spec
from herd.template import load_template, available_templates

from helpers import SOCK

# Only the tests that actually PARSE TOML are version-gated. tomllib is stdlib from
# 3.11 and template.py degrades to a friendly ValueError below that — documented in
# README and DESIGN — so these assert behaviour 3.9 cannot reach.
#
# Scoped, NOT module-level: the merge tests (resolve_spec, CLI-over-template
# precedence, available_templates) never touch tomllib, and they are the ones most
# worth running on the floor. A module-level skip would drop 7 good tests to silence
# 12 inapplicable ones.
#
# find_spec, not sys.version_info: it tests the condition template.py branches on
# rather than a proxy for it.
needs_tomllib = pytest.mark.skipif(
    importlib.util.find_spec("tomllib") is None,
    reason="templates need Python 3.11+ (stdlib tomllib)")


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
@needs_tomllib
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


@needs_tomllib
def test_unknown_key_is_rejected(templates):
    templates("bad", 'cwd = "/x"\nnope = 1\n')
    with pytest.raises(ValueError, match="unknown key 'nope'"):
        load_template("bad")


@needs_tomllib
def test_bad_type_value_rejected(templates):
    templates("bad", 'type = "window"\n')
    with pytest.raises(ValueError, match="type must be"):
        load_template("bad")


def test_missing_file_and_bad_name(templates):
    with pytest.raises(ValueError, match="no template"):
        load_template("ghost")
    with pytest.raises(ValueError, match="invalid template name"):
        load_template("../etc/passwd")


@needs_tomllib
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


@needs_tomllib
def test_cmd_spawn_uses_template(templates, monkeypatch, fresh):
    templates("review", REVIEW)
    seen = _capture(monkeypatch)
    assert cli.cmd_spawn(fresh(), ["-t", "review"]) == 0
    s = seen["spec"]
    assert s.job == "review" and s.launch_type == "pane"
    assert s.prompt.startswith("Review the current diff.")
    assert s.claude_args == ["--model", "opus"] and s.vars == {"HERD_CONTEXT": "review"}


@needs_tomllib
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


@pytest.mark.parametrize("key,value", [
    ("job", 42), ("cwd", 42), ("title", 42), ("prompt", 42),
    ("job", True), ("cwd", ["/a"]),
])
@needs_tomllib
def test_a_non_string_key_is_a_friendly_error(tmp_path, monkeypatch, key, value):
    """args/vars were type-checked and the string keys were not, so `job = 42`
    reached valid_job() as a TypeError traceback out of `herd spawn` — from a file
    the user hand-wrote, which is what this validation exists to prevent."""
    monkeypatch.setenv("HERD_TEMPLATES", str(tmp_path))
    rendered = value if not isinstance(value, list) else '["/a"]'
    (tmp_path / "t.toml").write_text(
        f"{key} = {str(rendered).lower() if isinstance(value, bool) else rendered}\n")
    with pytest.raises(ValueError, match="must be a string"):
        load_template("t")


# ── the 3.9 degradation itself, asserted on EVERY interpreter ────────────────
def test_a_missing_tomllib_reports_the_version_not_a_traceback(templates, monkeypatch):
    """The whole reason template.py imports lazily: below 3.11 a template must fail
    with a sentence a user can act on, not a ModuleNotFoundError out of the CLI.

    Nothing asserted this on any interpreter — on 3.11+ the branch is unreachable,
    and on 3.9 the tests that would hit it are skipped. So it is simulated here
    rather than left to the one version that cannot check the rest.

    __import__ is patched rather than sys.modules[...] = None: the latter raises a
    bare ImportError, while a genuinely absent module raises ModuleNotFoundError,
    which is what template.py catches. Faking the wrong exception would prove the
    handler works against a case that never happens."""
    import builtins
    templates("real", 'job = "x"\n')          # the file EXISTS — isolate the import
    real_import = builtins.__import__

    def no_tomllib(name, *a, **kw):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'", name="tomllib")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    with pytest.raises(ValueError, match=r"3\.11"):
        load_template("real")


def test_a_typo_is_reported_as_a_typo_even_without_tomllib(templates, monkeypatch):
    """Error PRECEDENCE. The interpreter check used to run before the file check, so
    on 3.9 `herd spawn -t typo` answered "templates need Python 3.11+" — true, and
    not the problem. It sent you to upgrade python over a misspelling."""
    import builtins
    real_import = builtins.__import__

    def no_tomllib(name, *a, **kw):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'", name="tomllib")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    with pytest.raises(ValueError, match="no template"):
        load_template("nonexistent")
    with pytest.raises(ValueError, match="invalid template name"):
        load_template("../etc/passwd")
