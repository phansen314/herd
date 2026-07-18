"""Load spawn templates — TOML presets under ~/.herd/templates that fill SpawnSpec
defaults. A template is just a second SpawnSpec source, merged UNDER the CLI flags
(herd.spawn.resolve_spec) — it never touches the DB or the executor. Format is TOML
for its triple-quoted multiline strings: a multiline `prompt` is the point.

TOML parsing uses stdlib tomllib (Python 3.11+). It is imported lazily so the rest
of herd still runs on 3.9/3.10 — only using a template needs 3.11.
"""
import os
import pathlib

# TOML key -> SpawnSpec field. Keys outside this map are rejected (typo protection).
_KEY_FIELD = {"cwd": "cwd", "type": "launch_type", "job": "job", "title": "title",
              "prompt": "prompt", "args": "claude_args", "vars": "vars"}


def _default_dir():
    """Env-overridable, ~/.herd default — read at call time so HERD_TEMPLATES set
    after import (and in tests) is honored. Same idea as daemon.DEFAULT_DB."""
    return os.environ.get("HERD_TEMPLATES", str(pathlib.Path.home() / ".herd" / "templates"))


def _dir(dir=None):
    return pathlib.Path(dir or _default_dir())


def valid_template_name(name):
    from herd.spawn import valid_job   # same filename/regex-clean charset (no / or ..)
    return valid_job(name)


def available_templates(dir=None):
    """Template names (basenames, no .toml) present in the dir — for completion."""
    d = _dir(dir)
    return sorted(p.stem for p in d.glob("*.toml")) if d.is_dir() else []


def load_template(name, *, dir=None):
    """Read <dir>/<name>.toml into a dict of SpawnSpec-field overrides. Raises
    ValueError (with a friendly message) on a bad name, missing file, bad TOML, or
    an unknown/mistyped key — never a bare stack trace on the CLI."""
    if not valid_template_name(name):
        raise ValueError(f"invalid template name {name!r}")
    try:
        import tomllib
    except ModuleNotFoundError:
        raise ValueError("templates need Python 3.11+ (stdlib tomllib)")
    path = _dir(dir) / f"{name}.toml"
    if not path.is_file():
        raise ValueError(f"no template {name!r} at {path}")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"template {name!r}: bad TOML — {e}")

    out = {}
    for k, v in raw.items():
        if k not in _KEY_FIELD:
            raise ValueError(f"template {name!r}: unknown key {k!r} "
                             f"(allowed: {', '.join(sorted(_KEY_FIELD))})")
        out[_KEY_FIELD[k]] = v
    # light shape checks — a clear error beats a mysterious failure downstream.
    if out.get("launch_type") not in (None, "tab", "pane"):
        raise ValueError(f"template {name!r}: type must be 'tab' or 'pane'")
    if "claude_args" in out and not (isinstance(out["claude_args"], list)
                                     and all(isinstance(a, str) for a in out["claude_args"])):
        raise ValueError(f"template {name!r}: args must be a list of strings")
    if "vars" in out and not (isinstance(out["vars"], dict)
                              and all(isinstance(v, str) for v in out["vars"].values())):
        raise ValueError(f"template {name!r}: [vars] must have string values")
    return out
