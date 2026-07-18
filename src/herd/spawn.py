"""herd spawn — launch a named claude session in kitty and record its placeholder.

The design seam is SpawnSpec: the CLI (now) and a template loader (later) both
produce one, and this single executor consumes it — so templates are just a file
that yields a SpawnSpec and never touch the DB or this code. Writes go through the
canonical W1 statements (load_statements), like every other write path.
See DESIGN.md#write-paths-schemawritessql.
"""
import os
import re
from dataclasses import dataclass, field

from herd.db import load_statements
from herd.kitty.launch import launch as _launch

W = load_statements()

# A job name becomes a kitty --tab-title, a HERD_JOB var (matched as an unanchored
# regex later), and herd_sessions.job_name. Keep it filename/regex-clean.
_JOB_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_job(job):
    return bool(job) and bool(_JOB_RE.match(job))


@dataclass
class SpawnSpec:
    """A fully-resolved spawn. Every field is individually overridable — this is
    the contract a template file will later fill (CLI flags override template)."""
    job: str
    cwd: str
    launch_type: str = "tab"          # tab | pane
    title: str = None                 # defaults to job
    prompt: str = None
    claude_args: list = field(default_factory=list)
    vars: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.title is None:
            self.title = self.job


def resolve_spec(cli, tmpl):
    """Merge CLI overrides over template defaults into a SpawnSpec. Precedence:
    CLI flag (non-None) > template value > built-in default. claude_args is the one
    list field — template args come first, CLI `-- args` append after (the template
    is the base, the CLI adds ad-hoc). Raises ValueError if no job resolves."""
    def pick(field, default=None):
        v = cli.get(field)
        return v if v is not None else tmpl.get(field, default)

    job = pick("job")
    if not job:
        raise ValueError("a job name is required (positional argument or template 'job')")
    cwd = os.path.abspath(os.path.expanduser(pick("cwd") or os.getcwd()))
    claude_args = list(tmpl.get("claude_args") or []) + list(cli.get("claude_args") or [])
    return SpawnSpec(job=job, cwd=cwd,
                     launch_type=pick("launch_type") or "tab",
                     title=pick("title"), prompt=pick("prompt"),
                     claude_args=claude_args, vars=dict(tmpl.get("vars") or {}))


def spawn(conn, spec, socket, now, *, launch_fn=None):
    """Reserve the job name, launch, then stamp the window. Returns (ok, msg, pk).
    launch_fn(spec, socket) -> window_id|None is injected for tests.

    RESERVE BEFORE LAUNCH. The obvious order (check -> launch -> insert) is a TOCTOU:
    the launch is a subprocess + kitty socket round trip, so two concurrent spawns of
    one name both pass the check and both insert. No unique index catches it (see
    W1 in writes.sql), so the claim has to be atomic in code. Taking the write lock
    with BEGIN IMMEDIATE before re-checking makes the loser block, then correctly
    see the winner's row. A rejection still never opens a tab."""
    launch_fn = launch_fn or _launch
    if not valid_job(spec.job):
        return False, f"invalid job name {spec.job!r} (use letters, digits, . _ -)", None
    if not socket:
        return False, "herd spawn needs to run inside kitty (KITTY_LISTEN_ON unset)", None

    # ── phase 1: claim the name, window unknown ──
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(W["R_job_live"], {"job": spec.job}).fetchone() is not None:
            conn.execute("ROLLBACK")
            return False, f"a live session already holds the job {spec.job!r}", None
        pk = conn.execute(W["W1_spawn_session"], {"cwd": spec.cwd, "now": now}).lastrowid
        conn.execute(W["W1_spawn_herd"],
                     {"pk": pk, "job": spec.job, "now": now, "socket": socket})
        conn.execute("COMMIT")
    except Exception as e:                       # noqa: BLE001 — degrade, never crash the CLI
        # BEGIN IMMEDIATE is INSIDE the try (it is the statement most likely to fail:
        # busy_timeout expiry under a concurrent writer). An unconditional ROLLBACK
        # would then raise "cannot rollback - no transaction is active" out of the
        # handler and crash the CLI it exists to protect. in_transaction is the only
        # honest test of whether there is anything to roll back.
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except Exception:                    # noqa: BLE001 — the reserve already failed
                pass
        return False, f"could not reserve the job {spec.job!r}: {e}", None

    # ── phase 2: launch, then stamp the placement onto the reservation ──
    # A RAISING launcher must be treated exactly like a failed one. subprocess.run
    # raises FileNotFoundError when kitten isn't on PATH (and OSError under a fork
    # limit); letting that propagate skips the abort below and strands the
    # reservation as a pid-NULL row the reaper's pid predicate never revisits, so
    # the job name stays burned until the next boot sweep.
    try:
        win, err = launch_fn(spec, socket), None
    except Exception as e:                       # noqa: BLE001 — degrade, never crash the CLI
        win, err = None, e
    if win is None:
        # drop the reservation, or the name stays taken by a session that never was
        try:
            conn.execute(W["W1_spawn_abort"], {"pk": pk})
        except Exception:                        # noqa: BLE001
            pass
        why = f": {err}" if err is not None else " (remote control off, or bad socket?)"
        return False, f"kitty launch failed{why}", None

    try:
        conn.execute(W["W1_spawn_window"], {"pk": pk, "win": win, "now": now})
    except Exception as e:                       # noqa: BLE001 — the tab IS open; keep it
        return False, f"launched window {win} but failed to record it: {e}", pk
    return True, f"spawned {spec.job!r} -> #{pk} in window {win}", pk
