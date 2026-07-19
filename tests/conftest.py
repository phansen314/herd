"""Fixtures for the herd suite.

- `fresh`  — a factory that returns an autocommit connection to a per-test temp
  DB with the schema applied. Autocommit matters: the hook tests read the DB from
  a separate process, and an uncommitted setup would be invisible to them (the
  false-pass that bit the original suite).
- `hook_env` — a temp DB + runtime dir + a `run()` that execs a REAL bash hook
  against them, exactly as production does. `.conn()` re-opens for assertions.
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                       # so `import helpers` works
sys.path.insert(0, str(HERE.parent / "src"))

from herd.db import apply_schema                      # noqa: E402
from helpers import HOOKS                             # noqa: E402


# ── external toolchain: SKIP what cannot run, don't fail it ──────────────────
# The hook tests exec real bash against real jq and sqlite3 — that fidelity is the
# point (a mocked hook proves nothing about the one production runs). But a
# contributor missing jq used to get a wall of subprocess failures that read like
# herd is broken, when the correct message is "this machine cannot run these".
#
# Two conditions, deliberately not one blanket check. A hook needs all three tools;
# a test that only shells out to bash (`bash -n`, sourcing common.sh) needs only
# bash, and skipping it for a missing jq would be a lie about coverage.
HOOK_TOOLS = ("bash", "jq", "sqlite3")
MISSING_HOOK_TOOLS = [t for t in HOOK_TOOLS if shutil.which(t) is None]
BASH_MISSING = shutil.which("bash") is None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "shell: shells out to bash directly (not via the hook fixtures)")


def pytest_runtest_setup(item):
    """Skip on a missing toolchain rather than failing. Fixture-driven for the hook
    tests (no per-test marker to forget), marker-driven for the direct callers."""
    if item.get_closest_marker("shell") and BASH_MISSING:
        pytest.skip("needs bash")
    if MISSING_HOOK_TOOLS and {"hook_env", "bash_stmt"} & set(getattr(item, "fixturenames", ())):
        pytest.skip(f"needs {', '.join(MISSING_HOOK_TOOLS)} (real hooks run here)")


def pytest_report_header(config):
    """Say it ONCE, up top. A run that silently skipped 300 tests looks the same as
    a run that passed them."""
    if MISSING_HOOK_TOOLS:
        return (f"herd: MISSING {', '.join(MISSING_HOOK_TOOLS)} — the hook tests will "
                f"SKIP. Install them for a full run; see CONTRIBUTING.md.")
    return None


def _open(path, tier2=True):
    c = sqlite3.connect(str(path))
    c.isolation_level = None                          # autocommit
    apply_schema(c, tier2=tier2)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def fresh(tmp_path):
    """fresh(tier2=True) -> a connection to a clean temp DB. Multiple calls in one
    test get distinct files (name them to disambiguate)."""
    made = []
    n = [0]

    def _fresh(tier2=True, name=None):
        n[0] += 1
        p = tmp_path / (name or f"f{n[0]}.db")
        c = _open(p, tier2=tier2)
        made.append(c)
        return c

    yield _fresh
    for c in made:
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture
def hook_env(tmp_path):
    """A DB file + runtime dir + a real-bash-hook runner."""
    dbp = str(tmp_path / "f.db")
    _open(dbp).close()
    runtime = str(tmp_path / "rt")
    os.makedirs(runtime)

    def run(script, payload, env=None, args=()):
        """`args` is for the scripts that take argv instead of a stdin payload
        (preview.sh). payload=None sends no stdin — a hook reading it would just
        parse nothing, which is the correct outcome for a payload that isn't ours."""
        e = dict(os.environ, HERD_DB=dbp, HERD_RUNTIME=runtime,
                 HERD_ERRLOG=f"{runtime}/err.log")
        # SCRUB the ambient kitty vars. The hooks branch on them, so inheriting the
        # developer's terminal makes "outside kitty" tests silently run INSIDE
        # kitty — one passed that way locally for weeks and only failed on a CI
        # runner, where there is no kitty. A test that needs them passes them in
        # `env`, which still wins because the update below comes after.
        for k in ("KITTY_WINDOW_ID", "KITTY_LISTEN_ON", "HERD_JOB"):
            e.pop(k, None)
        if env:
            e.update(env)
        return subprocess.run(["bash", str(HOOKS / script), *args],
                              input="" if payload is None else json.dumps(payload),
                              capture_output=True, text=True, env=e)

    def conn():
        c = sqlite3.connect(dbp)
        c.row_factory = sqlite3.Row
        c.isolation_level = None
        return c

    return SimpleNamespace(path=dbp, runtime=runtime, run=run, conn=conn)


@pytest.fixture(scope="session")
def bash_stmt():
    """Extract a statement via the bash stmt() helper (for the drift check)."""
    def _stmt(name):
        r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; stmt {name}'],
                           capture_output=True, text=True, env=dict(os.environ))
        return r.stdout
    return _stmt
