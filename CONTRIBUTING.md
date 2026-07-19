# Contributing to herd

## Getting set up

herd has no runtime dependencies, so there is nothing to install for the package
itself. What you do need is the toolchain the hooks actually run on:

| Tool | Needed for |
|---|---|
| `bash`, `jq`, `sqlite3` | the hooks — and therefore most of the test suite |
| Python ≥ 3.9 | the CLI, the daemon, the installer, the tests |
| `pytest` | the tests (`pip install -e '.[test]'`, or just `pip install pytest`) |
| `fzf`, kitty | `herd jump` / `herd watch` / placement — **not** needed for tests |

```bash
python3 -m pytest          # from the repo root
```

If `bash`, `jq` or `sqlite3` is missing, the suite **skips** the tests that need it
and says so in the header — it does not fail. You will still get a useful run
(~385 of 623 tests with all three absent), just not a complete one. Install the
missing tool before trusting a green result on hook changes.

## Working on hooks

By default the installer *copies* the hooks to `~/.herd/hooks`, so edits in the
checkout have no effect on running sessions until you re-install. For hook work:

```bash
PYTHONPATH=src python3 -m herd.install --dev   # wire the CHECKOUT directly
```

See [Hook development](README.md#hook-development---dev) for the tradeoff (a `git
checkout` then changes what live sessions execute, mid-turn). `herd doctor` reports
which mode is wired.

## Testing conventions

The hook tests exec **real bash** against **real jq and sqlite3**, driving the same
scripts production runs. That fidelity is deliberate: a mocked hook proves nothing
about the one Claude actually executes. Two consequences for anything you add:

- **Use the `hook_env` fixture** when your test runs a hook. It gives you a temp DB,
  a temp runtime dir, and a `run()` that execs the real script — and it carries the
  automatic skip when the toolchain is missing.
- **Mark direct `bash` callers** `@pytest.mark.shell` if you shell out without going
  through `hook_env`/`bash_stmt` (sourcing `common.sh`, `bash -n`, and so on). That
  marker means "needs bash" specifically; the fixtures mean "needs all three". Both
  exist so a missing `jq` never skips a test that would have run fine — a false skip
  is a lie about coverage.

**Write oracles that can fail.** A cautionary example lives in
`tests/test_statusline_e2e.py`: it once computed its expected value by shelling out
to `date -d @epoch +%-I:%M%p`. On BSD that returns empty, so the assertion silently
degraded to a substring that was true regardless — a test about portable formatting
passing vacuously on the platform it existed to protect. If your expected value comes
from a subprocess, ask what happens when that subprocess fails.

**Portability is a real constraint.** herd targets Linux and macOS. In shell, that
rules out GNU-only behavior: no `date -d`, `readlink -f`, `sed -i` without a suffix,
`stat -c`, or gawk extensions (`mktime`, `strftime`) — see the hand-rolled `epoch()`
in `statusline.sh` and the GNU/BSD `date` probe in `common.sh` for the house style.
Prefer POSIX; where you cannot, detect and fall back.

## Documentation

Three files, with distinct jobs — put the change in the right one:

- **README.md** — how to use herd.
- **DESIGN.md** — how it works, and the data model.
- **DECISIONS.md** — *why* it is built this way, including paths not taken.

The codebase comments heavily on the *why*, especially where something looks
gratuitously indirect (it usually got that way by breaking). If you fix a bug that
was hard to find, leave the reason behind — that convention is why several of these
bugs were only caught once.
