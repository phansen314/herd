# herd — decisions

Why things are the way they are, dated, newest first. [DESIGN.md](DESIGN.md)
describes the system as it *is*; this file records what was tried, measured, or
removed to get there.

Read this when you're about to "simplify" something and want to know whether
someone already did and reverted it. Each entry ends with **Protects:** — the
current behaviour that would break if the decision were undone.

**By topic:** [the pager](#pager) · [attention/ack](#ack) · [reaper robustness](#ps-floor) ·
[spawn TOCTOU](#toctou) · [the events table](#events) · [statement
transcription](#transcription) · [ctrl-q](#expect) · [the poker](#poker) ·
[pid ancestry](#spike1) · [the `live` column](#live-column) · [two clocks](#clocks) ·
[HERD_RUNTIME](#runtime) · [awk vs bash](#awk) · [kitty match semantics](#kitty-match)

---

## 2026-07-18 — No Claude-invoked pager; the escalation stub is deleted {#pager}

Considered giving herd a pager Claude could invoke — a CLI verb plus a skill, so a
session could raise itself deliberately instead of waiting for the derived `!`.
Rejected, along with the softer `herd say "<reason>"` variant that would have
attached a reason string rather than a notification.

**Rejected — redundant.** Claude already signals "done/blocked" by ending its turn:
`stop.sh` → `waiting` → terminal bell → `!` one threshold later. A command Claude
calls immediately before stopping fires seconds earlier carrying the same fact.

**Rejected — blind to the only real gap.** The one case ambient attention misses is
a session gone silently stuck in `working`. But a stuck session is stuck *inside a
tool call*; it is not deciding whether to invoke a CLI. Self-report cannot cover the
failure mode in which self-report is what broke. That case needs a daemon-side
actuator or nothing.

**Rejected — unreliable, therefore corrosive** (the decisive one). Claude would call
it only sometimes. A signal that is only sometimes emitted destroys the meaning of
its own absence: with ten sessions listed, you could no longer read a quiet row as
"nothing to report" rather than "didn't bother". That degrades the derived `!` sitting
next to it. Derived signals have no such failure mode — the daemon ticks every 2s
regardless of what Claude feels like doing.

**Decided:** sessions stay *observed*, never participants. Nothing Claude does
reaches herd except through the five hooks, and attention stays derived-only.

That settles the question `paged_at` / `paged_level` / `W6b_paged` were holding open.
They were schema and SQL with no production caller — `paged_level` was structurally
always `0`, so the preview could only ever print `(rung 0)` — kept warm for an
actuator that is now decided against. Deleted, same as the write-only events table
([#events](#events)). If a daemon-side notifier is ever built it brings its own
columns; keeping dead ones warm bought nothing but a misleading render.

**Protects:** attention being a binary armed/acked signal, the absence of any
session→herd channel, and `test_pager_actuator_stays_deleted` in
`tests/test_source_invariants.py`.

## 2026-07-18 — Ack is a timer restart, not a delete {#ack}

`ack_at` had been written on every jump since focus landed, and read by nothing:
the CLI rendered `!` from `attention_at` alone and the daemon's armed-check was
`session_pk IS NOT NULL`. Jumping did nothing visible.

**Considered and rejected:** have the jump delete the attention row. `W6d_rearm` is
a whole-row `DELETE`, so it discards `ack_at`; the next tick then measures silence
from the unchanged `last_event_at` — still past threshold — and re-arms. Measured as
a flap on every tick, ~2s apart, forever.

**Also rejected:** have the jump advance `last_event_at`. That is Claude's activity
clock and a jump is not Claude activity; writing it would corrupt the very signal
attention is derived from (see [two clocks](#clocks)).

**Decided:** an acked row stays in `herd_attention`. The CLI hides `!` while
`ack_at` is set, and the daemon re-notifies once a full status threshold of silence
has passed *since the ack*. Ack is a snooze, not a dismissal.

**Protects:** the third branch in `attention_tick`, and the fact that acked rows are
not deleted. Deleting them is the flap.

## 2026-07-18 — A failed `ps` must not read as an empty machine {#ps-floor}

`read_proc_table` ignored `ps`'s exit status. `_dead()` treats absence from the
table as death, so any `ps` failure — nonzero exit, fork limit, `ps` missing from
PATH under systemd — produced `{}` and reaped **every live session in one tick**.
A missing `ps` additionally killed the daemon with an uncaught `FileNotFoundError`.

**Decided:** return `None` rather than `{}` on a nonzero exit, an `OSError`, or an
unparseable table, and skip the reap for that tick. A real `ps -eo` always lists at
least the daemon itself, so an empty parse means the probe failed.

**Protects:** the `procs is not None` guard in `run()`. Reintroducing "just parse
whatever came back" restores a one-tick total wipe.

## 2026-07-18 — Reserve before launch {#toctou}

`herd spawn` originally ran `check R_job_live -> kitten @ launch -> INSERT`. The
launch is a subprocess plus a socket round trip — tens to hundreds of milliseconds
between the check and the write — so two spawns of one name both passed the check
and both inserted. Nothing corrupted, but the handle became ambiguous: `resolve()`
returned two rows, so `herd jump api` opened the picker instead of jumping, which is
exactly the scriptability the unique-match branch exists to provide.

**No index can catch this.** `job_name` must repeat across dead rows (that is what
makes names recyclable), so a plain `UNIQUE` is out. The constraint you want is
unique-among-live, but liveness is `sessions.stopped_at` while the name lives in
`herd_sessions`, and a SQLite partial index cannot reference another table. The
denormalized `live` column that would have made it expressible is the one removed in
[2026-07-16](#live-column).

**Decided:** make the claim atomic in code — `BEGIN IMMEDIATE`, re-check, insert with
`window_id` NULL, commit; then launch and stamp the window. Taking the write lock
*before* the check is the whole trick: the loser blocks, then sees the winner's row.

A later fix (same day) found the error handler could itself raise: `BEGIN IMMEDIATE`
is inside the `try`, so an unconditional `ROLLBACK` in the `except` threw
`cannot rollback - no transaction is active` and crashed the CLI it existed to
protect. Roll back only when `conn.in_transaction`.

**Protects:** `R_job_live` running *inside* the transaction, and the conditional
rollback. Moving the check before `BEGIN IMMEDIATE` reopens the race.

## 2026-07-18 — The events table was write-only {#events}

herd appended every lifecycle event to an `events` table. Nothing ever read it —
the signal is `sessions.last_event_at` — so it was clutter with a hazard attached:
with two writes, a guard on one and not the other would let `events` and `sessions`
silently disagree, and only `sessions` is read.

**Decided:** removed. One lifecycle write (`W4_event`). Historical/analytics needs
are served ad-hoc by parsing the per-session JSONL transcript
(`sessions.transcript_path`).

`PRAGMA auto_vacuum=INCREMENTAL` in `core.sql` was added for this table's churn and
outlived it — nothing else grows unboundedly and no `incremental_vacuum` is ever
called.

**Protects:** the single-lifecycle-write rule. A second event sink brings the
divergence hazard back.

## 2026-07-18 — Nothing keeps its own transcription of a write path {#transcription}

The predecessor re-typed statements inline in each hook; **four defects survived 40
checks** that way. Later, the CLI briefly kept two hand-written copies of the list
query: `R1_list` sat unused while the CLI's private copy missed the
`idx_sessions_live` plan the query test had been guarding all along.

**Decided:** every write and the one live read are named `-- :name` blocks in
`writes.sql`, loaded by both `herd.db.load_statements()` (python) and `common.sh`'s
`stmt()` (awk). The suite asserts the two extract the same text.

**Protects:** `test_hooks.py::test_bash_and_python_extract_same` and
`test_source_invariants.py::test_no_hook_inlines_dml`. Any inline SQL is the rot
these exist to prevent.

## 2026-07-18 — `ctrl-q` needs `--expect`, not a bind {#expect}

`herd watch` re-enters fzf on every exit, so quitting has to be distinguishable from
cancelling. **Every other exit collapses into Esc's outcome:** `abort` exits 130 with
empty stdout, so a `ctrl-q:abort` bind is indistinguishable from a cancel and `watch`
just loops. That shipped broken.

ctrl-c is worse than it looks: fzf puts the terminal in raw mode, which disables
ISIG, so ctrl-c never becomes a SIGINT — fzf reads the raw `0x03` itself.
`cmd_watch` still catches `KeyboardInterrupt`, but that only covers the "no live
sessions" sleep, where no fzf is running. fzf 0.44.1 has no `print(...)` action, so
`--expect` is the only route.

**Protects:** both quit keys going through `--expect`. A bind-based "simplification"
makes the dashboard unquittable.

## 2026-07-18 — Three measured facts about the poker {#poker}

- **`--listen=0` is not usable here.** fzf's `start` event does not reliably see
  `$FZF_PORT`, so a `start:execute-silent(… poke &)` bind spawned the poker on one
  picker and not the next — auto-refresh worked intermittently. `watch` picks the
  port itself (`_free_port`), which also makes it the poker's parent, so it can reap
  it in a `finally` instead of orphaning one per jump.
- **Reload only on change.** An unconditional 2s reload redraws the pane for nothing;
  the poker diffs the row text first.
- **But contact fzf every tick anyway** (`data=None` → a liveness GET). A poker that
  only spoke on change could not notice its fzf had exited while the herd was quiet.
  Early failures are tolerated (`_POKE_GRACE`): `watch` spawns the poker before fzf
  has bound the port, and treating that startup window as death killed auto-refresh
  outright.

**Protects:** the self-chosen port, the change diff, and the grace window.

## 2026-07-17 — pid comes from the ancestry walk; SPIKE-1 overturned {#spike1}

SPIKE-1 (a scratch document, never in this repo) concluded that Claude's pid had to
come from `kitten @ ls`. Verified live and overturned: the **blocking** SessionStart
hook is a live descendant of claude, so exactly one claude is an ancestor — other
sessions' claudes and claude's own MCP children are siblings, never on the upward
path. First-match-walking-up wins with no ppid cross-check.

Meaningful **only from a blocking hook**: an async hook can be reparented to init
(ppid → 1), breaking the chain.

**Protects:** `claude_pid()` / `_walk_claude()` in `common.sh`, and SessionStart's
registration as blocking.

## 2026-07-16 — The denormalized `live` column and its trigger {#live-column}

`sessions` carried a `live` column maintained by a trigger. It produced a permanent
desync on resume — the trigger fired on death and nothing reset it when a resume
revived the session — and it was the *only* reason tier 2 reached into tier 1.

**Decided:** removed. "Is this window/job held by a live session?" is a JOIN to
`sessions.stopped_at`. Recyclability of window/job handles falls out for free, which
is why the lookup indexes are plain and non-unique.

**Cost, accepted knowingly:** unique-among-live becomes inexpressible as an index,
which is what forced the code-level reservation in [spawn](#toctou).

**Protects:** `test_source_invariants.py::test_no_live_denormalization_column` and
`test_core_declares_no_triggers`.

## 2026-07-16 — Two mirrored ways to destroy the idle signal {#clocks}

Both were hit for real:

1. **Rendering `updated_at` as idle** (the predecessor did this). Statusline stamps
   it ~1/sec, so the column reads ~0s forever and the signal is worthless.
2. **Gating a lifecycle `UPDATE` on the status changing.** `post_tool_use.sh` is the
   hot path and always passes `status='working'`, so an `AND status IS NOT :status`
   guard suppressed the entire update — including `last_event_at`. Measured: **5
   consecutive tool calls matched 0 rows**, `last_event_at` never moved, and the
   silence rule then paged about a *busy* session.

**Rule that falls out:** any suppressor must gate on nothing that carries a clock.
This is why `W4_event` has no status guard.

**Protects:** `W4_event`'s lack of a status guard, and `statusline.sh` never touching
`last_event_at`.

## 2026-07-16 — Config via default-expansion only {#runtime}

`HERD_RUNTIME` was written once as an unconditional `${XDG_RUNTIME_DIR:-/tmp}`
assignment. It ignored the test's override and wrote throttle state into the real
`/run`, so a check passed on first run and failed on every run after.

**Decided:** every knob is `${X:-default}`, never unconditional assignment.
`HERD_DB=/tmp/x ./hook.sh` must write where told, or the tests cannot redirect the
program's state and end up testing the machine.

**Protects:** the redirectability every hook test depends on.

## 2026-07-16 — An awk fork beats pure bash for `stmt()` {#awk}

Measured: **0.7ms for the awk fork, 1.6ms for the pure-bash equivalent.** The
intuition that avoiding a fork is always cheaper is wrong at this size.

`stmt()` also stops at the first `;` for a second reason beyond parser symmetry:
prose after a statement contains things like `:pid MUST be …` that `bind()` would
otherwise try to substitute.

**Protects:** the awk implementation, and the first-`;` cut.

## 2026-07-17 — kitty `--match pid:N` matches the wrong pid {#kitty-match}

Measured: `--match pid:N` matches the *window's* pid (the login shell), never the
foreground claude. So herd resolves pid → window itself and focuses by `--match id:`.

Match values are **unanchored regexes** — `job` matches `job-2`, and `title:herd`
also hits `herd-2`. Anchor them. This also bit the documented kitty keybinding
recipe, where a `--type=background` launch gets no `KITTY_LISTEN_ON` and every
`kitten @` in the script fails silently, making the key look dead.

**Protects:** the `id:`-based focus path and every anchored `--match` in the docs.
