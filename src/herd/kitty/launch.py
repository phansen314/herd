"""Launch a new claude session in a kitty tab/pane — the spawn path's kitty IO.

Mirrors focus.py: pure argv construction split from the subprocess call, IO
injected (run_fn) so the logic is testable without a live kitty. `kitten @ launch`
prints the new window's id (an integer) on stdout; that id is the placement
window_id the SessionStart hook later adopts on (W2). See DESIGN.md#write-paths-schemawritessql.
"""
import os
import subprocess

CLAUDE_NAME = os.environ.get("HERD_CLAUDE_NAME", "claude")

# herd launches TABS and PANES only, never an OS window (DESIGN.md#tiers). kitty
# calls a split a "window" and a tab a "tab" — map our names onto kitty's --type.
_KITTY_TYPE = {"tab": "tab", "pane": "window"}


def build_launch_argv(spec, socket):
    """The `kitten @ launch` argv for a SpawnSpec. Pure. Options precede the
    program; claude_args thread verbatim; --prompt is appended LAST as claude's
    trailing positional (an initial message for the interactive session)."""
    # --var sets a kitty WINDOW user-var (visible to `kitten @ ls`, used for window
    # matching). --env sets it in the launched PROCESS, which is the only form the
    # SessionStart hook can read — and reading it is what makes adoption independent
    # of whether W1_spawn_window has committed yet. Both, deliberately: they address
    # different consumers. See DECISIONS.md#spawn-identity.
    argv = ["kitten", "@", "--to", socket, "launch",
            "--type", _KITTY_TYPE.get(spec.launch_type, "tab"),
            "--cwd", spec.cwd,
            "--tab-title", spec.title,
            "--var", f"HERD_JOB={spec.job}",
            "--env", f"HERD_JOB={spec.job}"]
    for k, v in (spec.vars or {}).items():
        argv += ["--var", f"{k}={v}"]
    argv += [CLAUDE_NAME, *spec.claude_args]
    if spec.prompt:
        argv.append(spec.prompt)
    return argv


# Bounded for the same reason as focus.py: `kitten @` against a socket with nothing
# listening blocks forever. A timeout reads as a failed launch, which spawn() already
# handles — it drops the reservation so the job name frees.
LAUNCH_TIMEOUT = 10


def _run(argv):
    """Run kitten, return stdout. A TimeoutExpired reads as a failed launch.

    OSError is deliberately NOT caught here, unlike focus.py: spawn() wraps this
    call and puts the real cause in the message ("kitten not found"), which beats
    the generic "remote control off, or bad socket?" this layer would produce."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=LAUNCH_TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        return ""                      # non-integer -> launch() returns None


def launch(spec, socket, *, run_fn=None):
    """Launch the session; return the new kitty window_id (int), or None on failure.
    kitten prints the id on success — anything non-integer means the launch failed
    (remote control off, bad socket, …).

    run_fn is injected the way focus.py injects list_fn/focus_fn. Without it this
    layer could only be tested by stubbing spawn(launch_fn=) one level up, so the
    stdout -> window_id conversion — the part that decides whether a launched tab
    is recorded or stranded — had no coverage at all."""
    out = ((run_fn or _run)(build_launch_argv(spec, socket)) or "").strip()
    try:
        return int(out)
    except (TypeError, ValueError):
        return None
