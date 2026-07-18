"""herd spawn — launch a named claude session in kitty and record its placeholder.

The design seam is SpawnSpec: the CLI (now) and a template loader (later) both
produce one, and this single executor consumes it — so templates are just a file
that yields a SpawnSpec and never touch the DB or this code. Writes go through the
canonical W1 statements (load_statements), like every other write path.
See DESIGN.md#write-paths-schemawritessql.
"""
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


def spawn(conn, spec, socket, now, *, launch_fn=None):
    """Refuse a taken/invalid job, launch, and record the W1 placeholder rows.
    Returns (ok, msg, pk). launch_fn(spec, socket) -> window_id|None is injected
    for tests. Guards run BEFORE any launch, so a rejection never opens a tab."""
    launch_fn = launch_fn or _launch
    if not valid_job(spec.job):
        return False, f"invalid job name {spec.job!r} (use letters, digits, . _ -)", None
    if not socket:
        return False, "herd spawn needs to run inside kitty (KITTY_LISTEN_ON unset)", None
    if conn.execute(W["R_job_live"], {"job": spec.job}).fetchone() is not None:
        return False, f"a live session already holds the job {spec.job!r}", None

    win = launch_fn(spec, socket)
    if win is None:
        return False, "kitty launch failed (remote control off, or bad socket?)", None

    try:
        conn.execute("BEGIN IMMEDIATE")
        pk = conn.execute(W["W1_spawn_session"], {"cwd": spec.cwd, "now": now}).lastrowid
        conn.execute(W["W1_spawn_herd"],
                     {"pk": pk, "job": spec.job, "now": now, "socket": socket, "win": win})
        conn.execute("COMMIT")
    except Exception as e:                       # noqa: BLE001 — degrade, never crash the CLI
        conn.execute("ROLLBACK")
        return False, f"launched window {win} but failed to record it: {e}", None
    return True, f"spawned {spec.job!r} -> #{pk} in window {win}", pk
