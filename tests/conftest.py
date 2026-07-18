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

    def run(script, payload, env=None):
        e = dict(os.environ, HERD_DB=dbp, HERD_RUNTIME=runtime,
                 HERD_ERRLOG=f"{runtime}/err.log")
        if env:
            e.update(env)
        return subprocess.run(["bash", str(HOOKS / script)], input=json.dumps(payload),
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
