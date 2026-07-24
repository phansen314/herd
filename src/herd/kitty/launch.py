"""Launch a new claude session in a kitty tab/pane — the spawn path's kitty IO.

Mirrors focus.py: pure argv construction split from the subprocess call, IO injected
(run_fn). `kitten @ launch` prints the new window's id on stdout; that id is the
placement window_id the SessionStart hook later adopts on (W2).
See DESIGN.md#write-paths-schemawritessql.
"""
import os
import subprocess

CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# TABS and PANES only, never an OS window (DESIGN.md#tiers). kitty calls a split a
# "window" — map our names onto kitty's --type.
_KITTY_TYPE = {"tab": "tab", "pane": "window"}


def build_launch_argv(spec, socket):
    """The `kitten @ launch` argv for a SpawnSpec. Pure. Options precede the program;
    claude_args thread verbatim; prompt is appended LAST as claude's trailing
    positional."""
    # Both --var and --env, deliberately: --var sets a kitty WINDOW user-var (seen by
    # `kitten @ ls`, used for window matching); --env sets it in the launched PROCESS,
    # the only form the SessionStart hook can read — which is what makes adoption
    # independent of whether W1_spawn_window has committed. DECISIONS.md#spawn-identity
    argv = ["kitten", "@", "--to", socket, "launch",
            "--type", _KITTY_TYPE.get(spec.launch_type, "tab"),
            "--cwd", spec.cwd,
            "--tab-title", spec.title]
    # A restart resumes a session by its known uuid, so it needs NO adoption and sets
    # no job: an empty spec.job omits HERD_JOB entirely, and the SessionStart(resume)
    # hook revives the dead row by session_id instead. spawn() always has a valid job.
    if spec.job:
        argv += ["--var", f"HERD_JOB={spec.job}",
                 "--env", f"HERD_JOB={spec.job}"]
    for k, v in (spec.vars or {}).items():
        argv += ["--var", f"{k}={v}"]
    argv += [CLAUDE_NAME, *spec.claude_args]
    if spec.prompt:
        argv.append(spec.prompt)
    return argv


# Bounded like focus.py: `kitten @` against a socket with nothing listening blocks
# forever. A timeout reads as a failed launch; spawn() drops the reservation.
LAUNCH_TIMEOUT = 10


class LaunchError(RuntimeError):
    """The launch failed, carrying kitten's OWN diagnosis where there is one.

    Exists because there was nowhere to put that diagnosis. `_run` returned stdout
    and discarded stderr — which is where kitten writes the actual cause — and
    `launch` collapsed every failure into None, so spawn() had nothing to report and
    guessed: EVERY failed launch said "(remote control off, or bad socket?)",
    including a bad --cwd, a bad --type, an unwritable directory and a stale socket.
    kitty's remote control is the hardest step in herd's setup, so that guess sent
    people to re-check the one thing that was usually already right.

    spawn() needs no new handling: it already treats a raising launcher exactly like
    a failed one (dropping the reservation so the job name is freed at once, rather
    than staying burned until W3f sweeps it) and already prints the exception. That
    path was there for `kitten` missing from PATH; this just gives it more to say."""


def _run(argv):
    """Run kitten and return its stdout. Raises LaunchError with kitten's own stderr
    on a nonzero exit or a timeout. OSError is deliberately NOT caught, as in
    focus.py: spawn() wraps this call and reports the real cause ("kitten not
    found")."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=LAUNCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise LaunchError(
            f"kitten @ launch timed out after {LAUNCH_TIMEOUT}s — a socket with "
            "nothing listening (a stale unix:/tmp/kitty-<pid>)?") from None
    if p.returncode != 0:
        # stderr first: that is where kitten explains itself. Newlines flattened —
        # this ends up on one CLI line.
        detail = (p.stderr or p.stdout or "").strip().replace("\n", " ")
        raise LaunchError(f"kitten @ launch exited {p.returncode}"
                          + (f": {detail}" if detail else ""))
    return p.stdout


def launch(spec, socket, *, run_fn=None):
    """Launch the session and return the new kitty window_id (int).

    Raises LaunchError on any failure — it does NOT return None. kitten prints the
    id on success, so non-integer output means the launch failed; returning None for
    that threw away the only evidence of WHY. run_fn is injected for testing."""
    out = ((run_fn or _run)(build_launch_argv(spec, socket)) or "").strip()
    try:
        return int(out)
    except (TypeError, ValueError):
        raise LaunchError(
            f"kitten @ launch printed no window id (got {out!r}) — is "
            "allow_remote_control enabled in kitty.conf?") from None
