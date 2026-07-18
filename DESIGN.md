# herd — design

How herd works and which invariants hold. Source comments carry one load-bearing
line and point here with `<doc>.md#<anchor>`; if you're about to write an essay in
the code, write it here instead.

Source of truth for behaviour is `schema/writes.sql` (the write paths) and the
`pytest` suite under `tests/` (the checks). This doc says what is true **now**;
[DECISIONS.md](DECISIONS.md) records what was tried, measured, or removed to get
here — check it before undoing something that looks redundant.

---

## The data model

### Two tiers {#tiers}

The schema splits along a strict, one-way boundary:

- **Tier 1 — `sessions`** (`schema/core.sql`): facts that would be
  true whether or not herd existed — a Claude process with this pid, cwd,
  status. `core.sql` may **never** mention `herd_*`. Enforced by
  `test_source_invariants.py::test_core_has_no_herd_tables` (scans comment-stripped
  DDL) and `test_schema.py::test_tier1_applies_standalone` (a herd-less install is
  a working install).
- **Tier 2 — `herd_sessions`, `herd_attention`** (`schema/herd.sql`): herd's
  *relationship* to a session — the job name it was spawned with, its kitty
  placement, whether herd decided it needs you.

Allowed dependency direction: **tier2 → tier1** (FK to `sessions(id)`, and
tier-2 code may write tier-1 facts). **tier1 → tier2 is forbidden.** `herd/hooks`
is herd's own code that writes tier-1 facts and *reads* tier-2 placement to route
an adopt — that is a routing read, not a tier violation. `db.py` is deliberately
tier-agnostic: it knows where SQL lives and how to open a connection, nothing
about meaning.

The daemon honours the same seam at the process boundary: the tier-1 reaper
always runs; the tier-2 attention tick is gated by `HERD_ATTENTION` (set
`0/false/no/off` for core-only collection — the `sessions` table stays
maintained, `herd_attention` is never touched, build your own tooling on top).

### The identity spine {#identity}

`sessions.id` is a surrogate `INTEGER PRIMARY KEY AUTOINCREMENT`. Tier 2 FKs
reference **this**, never `session_id`. That is what lets a session exist — with
a job and a placement — *before* Claude Code has reported its UUID.
Adoption is then a plain `UPDATE` of the nullable `session_id` column: no PK
rewrite, no cascade.

`session_id` (Claude's UUID) is `TEXT UNIQUE` and nullable; SQLite `UNIQUE`
ignores NULLs, so many unadopted rows coexist.

`AUTOINCREMENT`, not a bare rowid alias: a bare rowid recycles the highest id
after a delete. Sessions are soft-deleted today (`stopped_at`), so it never
bites — but the moment a prune job adds a real `DELETE`, a recycled id could
reattach to a stale `:pk` held mid-tick by the daemon or a picker. `sqlite_sequence`
makes the surrogate strictly monotonic. Cost is one sequence write per insert,
immaterial at the session insert rate.

### The two clocks — do not conflate {#two-clocks}

- `last_event_at` — **semantic** activity. Lifecycle hooks *only*.
- `updated_at` — **any** write, including every statusline tick (~1/sec).

The *gap* between them is herd's attention signal. `statusline.sh` MUST NOT
touch `last_event_at`.

**Any suppressor must gate on nothing that carries a clock.** This is why
`W4_event` has no status guard: gating the hot path's `UPDATE` on the status
*changing* freezes `last_event_at` for a busy session, which then reads as silent.
Both ways to destroy this signal were hit for real — see
[DECISIONS.md#clocks](DECISIONS.md#clocks).

There is **one** lifecycle write — `W4_event` on `sessions`. Historical/analytics
needs are served ad-hoc by parsing the per-session JSONL transcript
(`sessions.transcript_path`); an `events` table existed for this and was removed as
write-only ([DECISIONS.md#events](DECISIONS.md#events)).

### Liveness = `sessions.stopped_at`, from `ps`, one source {#liveness}

`stopped_at IS NULL` means live. It is read by JOIN everywhere; there is **no**
denormalized `live` column, and no trigger ([DECISIONS.md#live-column](DECISIONS.md#live-column)).
"Is this window/job held by a live session?" is a JOIN to `sessions.stopped_at`.
Recyclability of window/job handles falls out for free (a dead row and a live row
may share a `(socket, window_id)` or a `job_name`; the JOIN tells them apart; dead
rows keep their placement as history). This is why the lookup indexes are plain,
non-unique.

**Liveness comes from the process table, NEVER from kitty.** Absence from a
`kitten @ ls` is evidence about *placement*: a socket blip, an `ls` timeout,
`allow_remote_control` off, or a missed socket would each mass-reap every live
row at once. When a claude really dies, `ps` says so within a tick.

### pid = Claude's own pid {#pid}

`sessions.pid` is Claude's pid, found by walking the process tree **up** from the
hook to the first ancestor with `comm == claude` (`claude_pid()` /
`_walk_claude()` in `common.sh`). Not the window shell — that outlives claude and
would make the row immortal.

The **blocking** SessionStart hook is a live descendant of claude, so exactly one
claude is an ancestor (other sessions' claudes and claude's own MCP children are
siblings, never on the upward path) — first-match-walking-up wins with no ppid
cross-check. Meaningful **only from a blocking hook**: an async hook can be
reparented to init (ppid → 1), breaking the chain. Verified live against an earlier
conclusion that it had to come from kitty ([DECISIONS.md#spike1](DECISIONS.md#spike1)).

`idx_sessions_pid_live` (`UNIQUE(pid) WHERE stopped_at IS NULL AND pid IS NOT
NULL`) keeps at most one live row per pid, making the identity merge safe by
construction. `W2c_pid_claim` reaps any stale live holder of a pid *before* a new
claim stamps it — provably stale, because the hook runs as a descendant of the
claude that owns that pid *now*, so a different live row claiming it died
silently and its pid was recycled. Without this the new session's pid write would
fail the unique index and the error-swallowing hook would drop the session.

**Accepted caveat + boot sweep:** the unique-pid invariant holds only while pids
aren't recycled under a live row. After a reboot, rows left `stopped_at IS NULL`
can collide with a recycled pid. Closed without a schema change by the boot sweep
(`W3e`, run once at startup): reap live rows whose `started_at` precedes system
boot. A `pid_start_time` column would close it properly; deliberately deferred.

---

## Write paths (`schema/writes.sql`)

These named `-- :name X` blocks are the **only** statements that write. Both
`herd.db.load_statements()` (python) and `common.sh`'s `stmt()` (awk) extract
them the same way, terminating at the first `;`;
`test_hooks.py::test_bash_and_python_extract_same` asserts the two agree once
whitespace is normalized, so bash and python cannot drift. Nothing may keep its own
transcription of a write path ([DECISIONS.md#transcription](DECISIONS.md#transcription)),
enforced by `test_source_invariants.py::test_no_hook_inlines_dml`.

Inline comments inside a statement must not contain `;` (both parsers cut there;
`test_source_invariants.py::test_every_statement_is_complete` catches truncation
via `sqlite3.complete_statement()`).

| Statement | Purpose |
|---|---|
| `W1_spawn_session` / `W1_spawn_herd` | Phase 1 of a spawn: reserve the job name (UUID/pid/window not yet known). `status_source='reconcile'` is a white lie — the CHECK has no `'spawn'`; real provenance is `herd_sessions.source`. Driven by `herd spawn`. |
| `W1_spawn_window` | Phase 2: stamp the launched window onto the reservation. See [spawn](#spawn). |
| `W1_spawn_abort` | A failed launch drops the reservation — a real `DELETE` (cascading), so the name frees immediately and a session that never existed leaves no history. |
| `W2_adopt` | SessionStart adopts herd's placeholder row for this window, joining on `(socket, window_id)` from env. Writes Claude's signals into the core row. Idempotent (`AND session_id IS NULL`). |
| `W2b_insert` | Fallback when W2 matched nothing: upsert on Claude's UUID. Resume revives a stopped row (`stopped_at=NULL`, fresh pid); `started_at` preserved so duration is total age. |
| `W2c_pid_claim` | Runs first, own txn: reap any stale live holder of this pid so `idx_sessions_pid_live` is satisfiable. Excludes our own row. NULL pid → no-op. |
| `W2b_placement` | Tier-2 half of the W2b fallback: record the kitty window the hook stands in, so a user-started `claude` is a first-class tracked session. Only writer of `source='hook'`. pk via `SELECT session_id`, not `last_insert_rowid()` (which returns the INSERTed, not the ON-CONFLICT-updated, row). |
| `W3d_reap` | Reaper: mark one session stopped whose pid the daemon found dead. `status_source='pid'` (inferred). |
| `W3e_boot_sweep` | Boot sweep (see [pid](#pid)). |
| `W4_event` | Lifecycle: status + `last_event_*` in one statement — the single lifecycle write. **No status guard** (see [two clocks](#two-clocks)). |
| `W4_end` | The only hook-driven death (`status_source='hook'` — it *knows*, vs W3d's inference). |
| `W5_statusline` | The sink for **every** field the statusLine payload carries — metrics, plus `claude_code_version`, `output_style`, `git_worktree`, `original_cwd` — and `git_branch`, which the hook derives from its own `.git` walk. Nothing else writes these; a field absent from this statement is a column that stays NULL forever. UPDATE only (never creates a row — would resurrect stopped sessions / invent empty cwd). Never touches `last_event_*`. Resets_at arrives as unix epoch, converted to ISO in SQLite (zero date forks); `COALESCE` keeps prior value on NULL. The `prev_cost` pair (burn-rate delta, resampled >300s) is correct as written: an UPDATE's RHS sees the OLD row, so `prev_cost_usd` captures the previous total before this statement overwrites it. |
| `W5b_adopt` | Statusline adoption ("Path C"): statusline is a child of claude, inherits `KITTY_*` like a hook, so a reconciled session picks up metrics with no hooks wired. Same liveness JOIN as W2. |
| `W6a_arm` / `W6c_ack` / `W6d_rearm` / `W6d_rearm_sid` | Attention (see [attention](#attention)). `W6d_rearm_sid` is the UUID-keyed variant a hook needs (hooks lack the surrogate pk) — same keyed-two-ways pattern as W2 vs W2b. |
| `R1_list` | The **one** live-session read: `sessions` + `herd_sessions` + `herd_attention`, attention-first ordering. `ls`, the picker, `rows` and the preview all go through it. Selects **only what a renderer consumes** — see [banked columns](#banked-columns). |
| `W3f_sweep_stranded` | Reclaims a phase-1 spawn reservation whose claude never reached SessionStart (the launcher raised, claude died before its first hook, or the W5b adoption lost the `session_id` race). Such a row is `pid` NULL + `session_id` NULL, which `W3d_reap` skips by design while `R_job_live` still counts it live — so the job name stayed burned until the next boot sweep. Age-gated on `HERD_STRANDED_SECS` (a reservation is legitimately pid-NULL across the launch round trip). DELETE, not `stopped_at`, for `W1_spawn_abort`'s reason: this session never existed. |
| `R_job_live` | Spawn-time recyclable-handle check: does a *live* session already hold this job name? By JOIN, no unique index. Dead rows keep their `job_name` — reuse is by design, and resolution searches live sessions only. Run **inside** `spawn()`'s `BEGIN IMMEDIATE`, not before it: the check must be atomic with the reservation, or two concurrent spawns both pass it across the kitty launch and both insert. See [spawn](#spawn). |
| `R_statusline` | Render input: feeds only the burn rate (the `prev_cost` pair). One read per fingerprint miss. |

**Routing read, not data read:** in an adopt writer (W2, W5b, W2b_placement) a
`herd_*` reference may appear only *after* `WHERE` (to pick which row), never in
the SET list. No tier-2 value ever enters a core column.

### Banked columns — written, deliberately unread {#banked-columns}

Some columns have **no reader today**, and that is a decision rather than an
oversight. Two groups:

**Rendered from the payload, banked in the DB.** The rate-limit pair, `model`,
`api_duration_ms` and `git_branch` are on screen every tick — but the statusline
renders them from the payload it just parsed, not from the row. The stored copy is
history, not display state.

**Never surfaced at all.** `total_input_tokens`, `total_output_tokens`,
`context_window_size`, `lines_added`, `lines_removed`, `exceeds_200k_tokens`,
`claude_code_version`, `output_style`, `original_cwd`, `git_worktree`. Nothing
reads these. They are banked because **the payload is the only source and it is
gone the moment the tick ends** — a question you think of next month ("what did
context growth look like before that refactor?") is unanswerable unless the data
was captured now.

This is not in tension with [the events table](DECISIONS.md#events), which was
removed for being write-only. A write-only *table* cost a second write path, its
own transaction and its own drift risk. A write-only *column* on a row `W5` is
already updating costs one more assignment in an UPDATE that was happening anyway.
The events table was a second thing to keep in sync; these are not.

What is **not** acceptable is paying for unread data on the *read* path. `R1_list`
runs on every `ls` and every `watch` refresh, so it selects only what a renderer
consumes; `herd_var`, `source` and `verified_at` were dropped from it for that
reason, while remaining written and governed by the mutability contract above.
`test_source_invariants.py::test_core_writers_take_no_tier2_value` enforces this
structurally: it splits each `sessions` writer at the first `WHERE` and greps only
the value region.

The liveness JOIN inside the adopt subquery (`s.stopped_at IS NULL`) is
load-bearing: a window outlives the claude in it, so a dead predecessor row still
owns the `(socket, window_id)`. Without the filter, adoption binds Claude's UUID
to the stopped row while the live session stays unadopted and invisible.

**`herd_sessions` mutability contract:**

| immutable (set once at spawn) | mutable (a hook re-fire may rewrite) |
|---|---|
| `job_name`, `created_at` — the job identity | `kitty_socket`, `window_id` — placement moves |
| `herd_var` — a hook cannot know the spawn var | `verified_at` — re-derivation stamp |
| `source` — provenance must not decay to `'hook'` | |

Enforced by discipline — name columns in the UPDATE, never blanket-overwrite — and
asserted by `test_mutability.py::test_refire_mutability_contract`, which re-fires
the hook from a *new* window and checks each column above. `W2b_placement` omits
the immutable set, so a resumed spawned session never loses its job identity.

---

## The hooks (`hooks/*.sh`)

Bash 3.2 compatible (macOS froze `/bin/bash` at 3.2): no associative arrays, no
`${var^^}`, no `mapfile`, no `printf '%(%s)T'` — indexed arrays and `printf -v`
only.

**Nothing may block Claude.** Every hook exits 0 unconditionally — `Stop`'s exit
2 would literally prevent Claude from finishing its turn, and a herd bug must
never be able to do that. Failures go to the error log, silent. (Exit 1 is used
only for the "can't even source `common.sh`" case: a non-blocking loud failure
whose stderr shows in the transcript.)

**Config via default-expansion only** (`${X:-...}`), never unconditional
assignment: `HERD_DB=/tmp/x ./hook.sh` must write where told, or the tests can't
redirect the program's state and end up testing the machine
([DECISIONS.md#runtime](DECISIONS.md#runtime)).

**`BASH_SOURCE%/*` footgun:** it returns the string *unchanged* when the script
is invoked with no directory component (`bash stop.sh`), which would leave every
helper undefined and make the hook a silent no-op that reports success. Every
hook guards: `__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."`.

### `common.sh` internals

- **`db()`** runs `sqlite3` with `-bail`, `.timeout 3000`, `foreign_keys=ON`,
  `synchronous=NORMAL`. `-bail` is load-bearing for `run_tx`: without it the CLI
  prints a statement error, *skips to the next statement, and runs COMMIT anyway*
  — half-committing before the failure while still exiting nonzero. With `-bail`
  it stops at the error, never reaches COMMIT, and the open transaction rolls
  back on exit. `busy_timeout` is not optional: WAL serialises writers, and
  without it a hook fails outright the moment the daemon holds the write lock.
- **`stmt()`** extracts one `-- :name X` block, stopping at the first `;` —
  which also keeps prose like `:pid MUST be ...` away from `bind()`. The awk fork
  is the measured-faster implementation ([DECISIONS.md#awk](DECISIONS.md#awk)).
- **`bind()`** expands `:name` params from `HERD_P_<name>` env vars in a **single
  pass**. Not sqlite3's `.param set`: its dot-command parser uses shell-like
  quoting, so a correct SQL escape (`o''brien`) mis-tokenizes, the param is left
  unbound, and sqlite3 silently binds NULL — silent data loss. Single-pass
  because sequential `${sql//:name/value}` rescans its own output (a cwd
  containing the literal `:now` gets mangled by the next substitution). Values
  travel via the environment, so no shell quoting is involved. Empty value →
  `NULL`; unknown param → hard failure (nonzero exit), never a silent NULL.
- **`run_tx`** wraps N statements in one `BEGIN IMMEDIATE ... COMMIT`, one fork,
  one WAL commit. Two callers: `stop.sh` (status change + attention re-arm) and
  `session_start.sh` (`W2b_insert` + `W2b_placement`). `BEGIN IMMEDIATE`, never
  plain `BEGIN`: a deferred txn upgrades to a write lock lazily and can throw
  `SQLITE_BUSY_SNAPSHOT`, which the busy timeout cannot retry away. All binding
  happens before any SQL runs, so an unbound param aborts with nothing executed.
- **`valid_sid()`** — a `session_id` becomes a filename (throttle/cache); reject
  anything with `/` or `..` so a payload can't escape `$HERD_RUNTIME`.
- **`now_pair()`** emits ISO + epoch from a *single* `date` fork (the throttle
  needs epoch, the write needs ISO); the GNU-vs-BSD format is probed once at
  source time.

### Per-hook notes

- **`session_start.sh`** — captures pid (`W2c_pid_claim` first, own txn), then
  W2 adopt by window, else `W2b_insert` (+ `W2b_placement` when in kitty, one
  txn). `source=startup|resume|clear|compact`; resume/compact are the same
  session continuing. `.model` is a *string* here (an *object* `{id,...}` on the
  statusline payload — same word, different shape).
- **`post_tool_use.sh`** — hot path, fires per tool call. Cannot skip the DB
  (must advance `last_event_at` or a busy session reads silent), so it
  **throttles** instead: one write per `HERD_TOOL_THROTTLE` (2s) window via a
  tmpfile epoch. The silence rule works in minutes, so 2s of staleness is
  invisible.
- **`stop.sh`** — the `waiting` signal (turn ended). Also `W6d_rearm_sid` in the
  same txn: a new semantic event clears the attention row so the rule may trip
  fresh — this is what makes ack mean "I've seen *this* silence", not "never
  bother me again".
- **`notification.sh`** — only `notification_type=permission_prompt` →
  `needs_approval`. `idle_prompt` is ignored: `stop.sh` already owns `waiting`.
- **`session_end.sh`** — the hook-driven death. **Must be registered blocking**:
  an async hook can be killed when the session exits, leaving `stopped_at` NULL.
  On `/clear` Claude emits SessionEnd then SessionStart for the new session in
  the *same* window; the death should land first.

### `statusline.sh`

Fires ~1/sec/session — must be fork-light. Two jobs: sink metrics to the DB and
render a two-line emoji status. Renders **every** tick (a statusLine command must
print), but does DB work only on a **fingerprint change** — a per-session tmpfile
holds `FP\nL1\nL2`; an unchanged payload costs zero sqlite3 forks. The fingerprint
covers every rendered/sunk field, so a cache hit can never show a stale line.
Cache write is tmp+rename (a torn write must not feed a false hit).

`⬢ name` renders Claude's `session_name` (a tier-1 payload fact), deliberately
not herd's tier-2 job name, so the render stays tier-1 pure. Parses one `jq` into
`\x1f`-separated fields (`\x1f`, not tab: tab is IFS whitespace and collapses
empty fields, shifting every later one). Git branch is a pure-bash walk to
`.git/HEAD` (zero forks). Burn rate is herd's own addition; the awk guards
`mktime` returning -1 on an unparseable stamp (else a bogus `$0.00/h`), and
sub-cent rates are hidden as noise.

---

## Spawn — reserve before launch (`spawn.py`) {#spawn}

`herd spawn` gives a session a name *before* Claude has reported a UUID, so the job
name is the handle for everything downstream. That makes "one live session per job
name" a guarantee worth actually holding.

The claim cannot be made by an index: `job_name` must repeat across dead rows (that
is what makes names recyclable), and unique-among-live would need a partial index
referencing another table, which SQLite cannot do. So it is made atomic in code, in
two phases ([DECISIONS.md#toctou](DECISIONS.md#toctou) for the race this closes):

1. **Reserve** — `BEGIN IMMEDIATE`, re-check `R_job_live`, insert both rows with
   `window_id` NULL, `COMMIT`. Taking the write lock *before* the check is the whole
   trick: a second spawner blocks, then sees the winner's committed row.
2. **Stamp** — launch, then `W1_spawn_window` records the window id.

A failed launch runs `W1_spawn_abort` (a real `DELETE`, cascading) so the name frees
immediately — the session never existed and must not linger in history. `sessions.id`
is `AUTOINCREMENT` precisely so that delete can never cause surrogate reuse.

Phase 1's error handler rolls back only when a transaction is actually open:
`BEGIN IMMEDIATE` is itself the statement most likely to fail, and an unconditional
`ROLLBACK` then raises out of `spawn()` and crashes the CLI it exists to protect.

The intermediate state — reserved, not yet placed — is safe to observe: `window_id`
NULL is already the "no window to focus yet" case in `focus_session`, and
`herd_sessions.window_id` is MUTABLE by contract.

### Templates {#templates}

`herd spawn -t <name>` loads `~/.herd/templates/<name>.toml` (override:
`HERD_TEMPLATES`) into a dict of `SpawnSpec` field overrides. A template is just a
**second source of SpawnSpec defaults** — `template.py` never touches the DB, kitty,
or `spawn()`; `resolve_spec` merges and hands the result to the same executor a bare
CLI invocation uses. That keeps the feature entirely in front of the write path.

Precedence is **CLI (non-None) > template > built-in default**, which is why
`--type` defaults to `None` rather than `"tab"`: the resolver has to distinguish
"unset" from an explicit `--tab`, or a template's `type` could never win. `args` is
the one field that concatenates instead of overriding — template args first, then
the CLI's `-- …` — so a template carries a base set and the CLI adds ad-hoc flags.
`job` may come from either side, so `herd spawn -t review` with no positional is
valid; failing to resolve one is a `ValueError`. A `[vars]` table adds kitty user
vars to the launched window alongside the `HERD_JOB` var herd sets itself; it has no
CLI flag, template-only.

TOML, not JSON, for triple-quoted multiline strings — a multiline `prompt` is the
motivating case. `tomllib` is imported lazily so only *using* a template requires
Python 3.11; the rest of herd stays on 3.9. Unknown keys raise rather than being
ignored (a mistyped `promt` should say so, not silently do nothing), and `load_template`
raises `ValueError` with a friendly message for every failure mode — bad name, missing
file, bad TOML, wrong type — so the CLI prints `✗ …` instead of a stack trace.

---

## Focus / jump (`kitty/focus.py`, `cli.py`)

Placement (`kitty_socket`, `window_id`) is a **cache, not a fact**. Before every
jump, `focus_session` re-derives the window from `kitten @ ls` and confirms the
window's foreground claude carries the session's stored pid — so it never focuses
a window reused after the session's window closed. Falls back to the stored
`window_id` when the pid can't be located, and self-heals the stored value when
it has drifted. A kitty restart invalidates every `window_id`; re-derivation
makes that invisible rather than fatal.

kitty's `--match pid:N` matches the *window's* pid (the login shell), never the
foreground claude, so herd resolves pid → window itself and focuses by `--match id:`
([DECISIONS.md#kitty-match](DECISIONS.md#kitty-match)). `(socket, window_id)` is the
whole jump key: `focus-window --match id:N` on a window in a background tab
activates that tab and returns 0, so no `tab_id`/`os_window_id` is needed.

User vars (`--var HERD_JOB=x`, matched `--match var:HERD_JOB=^x$`) are
window-scoped, sticky, and survive claude exiting — they identify a *window*,
never a session, so always AND with pid liveness before believing a match. Match
values are unanchored regex — anchor them or `job` matches `job-2`.

IO (`_ls`/`_focus`, `list_fn`/`focus_fn`) is injected so logic is testable
without a live kitty — the same discipline as `daemon.py` and `common.sh`.

There is exactly one live-session read: `R1_list`, loaded from `writes.sql` like
every other shipping statement. `cli._live()` *is* that statement, and `ls`, the
picker, `rows` and the preview pane all go through it — `preview` filters it by
id in Python rather than issuing a second query
([DECISIONS.md#transcription](DECISIONS.md#transcription)).

`cli` surface: `ls`, `jump`, `spawn`, `watch` and `doctor` are the user verbs;
`preview` (fzf's per-highlight pane), `complete` / `tcomplete` (tab-completion
feeds), `rows` (fzf's reload source) and `poke` (watch's refresh child) are
machinery — callable, hidden from help/completion. `doctor` is the one verb that
does NOT take the shared connection: a missing or corrupt DB is something it
reports, so opening one up front would break it on exactly the machines it exists
to diagnose. `jump` focuses immediately on a unique match (scriptable), else
opens an fzf picker with a live preview; without fzf/tty it prints the list. A
jump *is* an ack (`W6c_ack`).

### `watch` — fzf *is* the TUI {#watch}

`herd watch` is the dashboard: one kitty tab running the jump picker forever.
There is deliberately **no curses layer**. fzf already renders the list,
navigates it, and — because it spawns `preview` as a fresh process per highlight
— shows per-session detail that reads live from SQLite for free. A TUI would only
be a second rendering path to keep in sync with `ls`.

Two things turn a picker into a dashboard:

*The loop.* fzf exits on every pick and every cancel, so `watch` re-enters it.
Esc therefore re-opens the picker rather than dropping you to a prompt — the tab
is something you cannot accidentally fall out of. `ctrl-q` and `ctrl-c` are the
way out.

Both quit keys go through `--expect`, which prints the pressed key as stdout's
first line — every other exit collapses into Esc's outcome, and ctrl-c never
reaches Python because fzf's raw mode disables ISIG
([DECISIONS.md#expect](DECISIONS.md#expect)). `cmd_watch` still catches
`KeyboardInterrupt`, but that only covers the "no live sessions" sleep, where no
fzf is running and the terminal is in normal mode.

*The poker.* Only the row list needs refreshing, and fzf cannot refresh it on a
timer, so `cmd_poke` runs alongside and POSTs `reload` to fzf's `--listen` port.
It is stdlib `urllib`, not `curl` — herd ships no runtime deps, and the poker is
long-lived so there is no cold start to pay. `watch` chooses the port itself
(`_free_port`) and so is the poker's parent; the poker diffs the row text and
reloads only on change, but contacts fzf every tick regardless so it notices an fzf
that exited while the herd was quiet. Early failures are tolerated (`_POKE_GRACE`).
All three were measured ([DECISIONS.md#poker](DECISIONS.md#poker)).

---

## Attention {#attention}

"Needs attention" is **derived every tick** from a session's status + how long
it's been in it (`now - last_event_at`), never stored as a flag. What persists in
`herd_attention` is the **action/edge**, not the opinion — because paging is a
side effect in the world, and without memory a 1s poll would page you 60×/min
about one stuck session.

Page-worthy statuses, their grace (seconds, env-overridable), and the mark each
renders in `ls` / the picker:

| status | grace | mark | already covered by Claude's bell? |
|---|---|---|---|
| `waiting` | 30s | 🙋 | yes — the turn ended, so Claude rings |
| `needs_approval` | 15s | 🔐 | yes — the prompt rings |
| `working` | 300s | 🥱 | **no** — a stuck session never ends its turn, so it never rings |

Statuses not listed (`stopped`, `unknown`) are never page-worthy.

The marks differ **because the statuses are not equivalent**. Two of the three
already reach you ambiently; the third is the only one herd tells you something you
could not otherwise learn, and it is the case a herd-owned notifier was going to
exist for. Rather than build the actuator, the mark is made legible and the answer to
"is anything wedged?" is `herd watch` on a dedicated tab
([DECISIONS.md#pager](DECISIONS.md#pager)).

Rendering rules (`cli.ATTENTION_MARKS` / `ATTENTION_REASONS`): every glyph occupies
exactly **two terminal cells**, and a quiet row reserves the same width, so the `#id`
column never goes ragged. Emoji width is not obvious by eye — `✓` is one cell, `✅`
is two — so it is asserted, not trusted
(`test_source_invariants.py::test_attention_glyphs_are_two_cells`). The mark set and
the daemon's threshold table must cover the same statuses, also asserted; the picker
falls back to ❗ if an armed row is somehow seen under another status.

- `attention_at` is the **edge**: when the rule first tripped (not the same as
  `last_event_at`'s age). `W6a_arm` uses `COALESCE` to preserve it across ticks.
- `W6c_ack` (written by a jump — acking is implicit, there is no dismiss verb and
  none is wanted) guards on `attention_at <=
  focus_started_at`, closing a race where a hook raising a *new* attention
  mid-jump would be acked unseen.
- `W6d_rearm` deletes the whole row — a `DELETE` is a meaningful "shut up and
  re-evaluate everything". Kept in its own table (not columns on `herd_sessions`)
  because it's the only thing written on every tick, and isolating that write
  keeps contention off the read-mostly rows.

### Ack is a timer restart, not a delete {#ack}

A jump acks the row. The CLI hides the mark while `ack_at` is set, but the row **stays
armed** — and that is deliberate, because `ack_at` is the only stamp that can
restart the silence timer.

Jumping cannot simply advance `last_event_at`: that is Claude's activity clock, and
a jump is not Claude activity (see [Two clocks](#two-clocks)). Nor can the jump just
delete the row: `W6d` is a whole-row `DELETE`, so it discards `ack_at`, and the next
tick measures from the unchanged `last_event_at` — already past threshold — and
re-arms immediately. That flaps every tick, forever.

So the tick has three branches, all reusing `W6a`/`W6d`:

| State | Action |
|---|---|
| silent past threshold, not armed | `W6a_arm` |
| no longer silent, armed | `W6d_rearm` — real activity wins |
| silent, armed, **acked**, and that much silence has passed *since the ack* | `W6d_rearm`; the next tick's `W6a` re-arms fresh with `ack_at` NULL |

The third branch is the re-notification: look at a session, answer nothing, and it
speaks up again one threshold later. Same per-status knobs, no new statement, no new
column.

One known gap: reaching a session **without** `herd jump` — clicking the tab, kitty
keyboard nav — writes no ack, so the mark persists until real activity. Closing it would
mean the daemon polling `kitten @ ls` for the focused window, which breaks
[liveness comes from ps, never kitty](#liveness). Left open on purpose; revisit with
the notifier.

Actually *notifying* you (notify-send, a kitty tab poke, escalation rungs) is a
separate actuator, deliberately deferred — this layer maintains the signal, which is
binary: armed or acked. Ambient attention is currently Claude's terminal bell plus
kitty's tab flag, which is outside herd entirely.

A Claude-invoked pager was considered as the actuator and rejected: a signal Claude
emits only sometimes destroys the meaning of its own absence, which is exactly what
the derived rule above is for. See [DECISIONS.md#pager](DECISIONS.md#pager).

---

## Testing

The `pytest` suite under `tests/` is the only CI gate: import-linter can't see
the tier boundary because that boundary is SQL and the hooks are bash. It applies
the real schema, loads the real statements through `herd.db`, and runs the
**real** hooks against a per-test throwaway DB. (`conftest.hook_env` invokes them
as `bash <path>`; the executable bit is covered separately by a source invariant,
and `install.selftest()` is what execs them the way production does.)

    python3 -m pytest              # whole suite
    python3 -m pytest tests/test_hooks.py     # one section
    python3 -m pytest -k pid       # by keyword

Layout: `tests/helpers.py` holds fixed clocks + row builders (`mk_session`,
`mk_herd`, …); `tests/conftest.py` the `fresh` (temp-DB connection) and
`hook_env` (real-bash-hook runner) fixtures. Each `test_*.py` maps to one concern
(tier boundary, two clocks, reaper, attention, focus, …). Autocommit on the setup
connection is load-bearing: the hook tests read the DB from a separate process,
and an uncommitted setup would be invisible to them.

---

## Deferred / not yet built

- A `pid_start_time` column to close the reboot pid-reuse caveat properly.
- Reaching a session without `herd jump` writes no ack — see [ack](#ack).
