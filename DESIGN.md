# herd — design

Rationale that used to live in source comments. Inline comments now carry one
load-bearing line and point here with `DESIGN.md#anchor`. If you're about to
write an essay in the code, write it here instead.

Source of truth for behaviour is `schema/writes.sql` (the write paths) and
`the test suite` (the checks). This doc explains *why* those look the way they do.

---

## The data model

### Two tiers {#tiers}

The schema splits along a strict, one-way boundary:

- **Tier 1 — `sessions`, `events`** (`schema/core.sql`): facts that would be
  true whether or not herd existed — a Claude process with this pid, cwd,
  status. `core.sql` may **never** mention `herd_*`. Enforced by `the test suite`
  (check A scans comment-stripped DDL; another check applies `core.sql`
  standalone).
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
a job, a placement, pager state — *before* Claude Code has reported its UUID.
Adoption is then a plain `UPDATE` of the nullable `session_id` column: no PK
rewrite, no cascade.

`session_id` (Claude's UUID) is `TEXT UNIQUE` and nullable; SQLite `UNIQUE`
ignores NULLs, so many unadopted rows coexist.

`AUTOINCREMENT`, not a bare rowid alias: a bare rowid recycles the highest id
after a delete. Sessions are soft-deleted today (`stopped_at`), so it never
bites — but the moment a prune job adds a real `DELETE`, a recycled id could
reattach to a stale `:pk` held mid-tick by the TUI/pager. `sqlite_sequence`
makes the surrogate strictly monotonic. Cost is one sequence write per insert,
immaterial at the session insert rate.

### The two clocks — do not conflate {#two-clocks}

- `last_event_at` — **semantic** activity. Lifecycle hooks *only*.
- `updated_at` — **any** write, including every statusline tick (~1/sec).

The *gap* between them is herd's attention signal. `statusline.sh` MUST NOT
touch `last_event_at`.

Two mirrored failure modes, both fatal:

1. If statusline ages `updated_at` and you render *that* as idle (the predecessor
   did), the column reads ~0s forever because statusline stamps it constantly —
   the signal is worthless.
2. If a lifecycle hook's `UPDATE` is gated on the status *changing*, a busy
   session emitting the same status repeatedly stops advancing `last_event_at`
   and reads as **silent**. `post_tool_use.sh` is the hot path and always passes
   `status='working'`, so an `AND status IS NOT :status` guard suppresses the
   entire update — including `last_event_at`. Measured: 5 consecutive tool calls
   matched 0 rows, `last_event_at` never moved, the silence rule then pages you
   about a *busy* session. **Any suppressor must gate on nothing that carries a
   clock.** This is why `W4_event` has no status guard.

`events` is append-only and **nothing reads it** (the TUI reads
`sessions.last_event_at`; `MAX(timestamp)` per session per tick over an unbounded
table is a cost we don't pay). Because nothing reads it, it can't corroborate
`last_event_at`: any guard on the lifecycle `UPDATE` must apply identically to
the `events` `INSERT`, or to neither.

### Liveness = `sessions.stopped_at`, from `ps`, one source {#liveness}

`stopped_at IS NULL` means live. It is read by JOIN everywhere; there is **no**
denormalized `live` column. There used to be one, maintained by a trigger on
`sessions` — it produced a permanent desync on resume (the trigger fired on
death, nothing reset it when a resume revived the session) and it was the *only*
reason tier 2 reached into tier 1. Removed. "Is this window/job held by a live
session?" is a JOIN to `sessions.stopped_at`. Recyclability of window/job handles
falls out for free (a dead row and a live row may share a `(socket, window_id)`
or a `job_name`; the JOIN tells them apart; dead rows keep their placement as
history). This is why the lookup indexes are plain, non-unique.

**Liveness comes from the process table, NEVER from kitty.** Absence from a
`kitten @ ls` is evidence about *placement*: a socket blip, an `ls` timeout,
`allow_remote_control` off, or a missed socket would each mass-reap every live
row at once. When a claude really dies, `ps` says so within a tick.

### pid = Claude's own pid {#pid}

`sessions.pid` is Claude's pid, found by walking the process tree **up** from the
hook to the first ancestor with `comm == claude` (`claude_pid()` /
`_walk_claude()` in `common.sh`). Not the window shell — that outlives claude and
would make the row immortal.

This overturns SPIKE-1 (which concluded pid must come from `kitten @ ls`): the
**blocking** SessionStart hook is a live descendant of claude, so exactly one
claude is an ancestor (other sessions' claudes and claude's own MCP children are
siblings, never on the upward path) — first-match-walking-up wins with no ppid
cross-check. Meaningful **only from a blocking hook**: an async hook can be
reparented to init (ppid → 1), breaking the chain.

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
them the same way, terminating at the first `;`; `the test suite` asserts the two
agree character-for-character, so bash and python cannot drift. Nothing may keep
its own transcription of a write path — the predecessor re-typed statements
inline and four defects survived 40 checks that way.

Inline comments inside a statement must not contain `;` (both parsers cut there;
`sqlite3.complete_statement()` in the suite catches truncation).

| Statement | Purpose |
|---|---|
| `W1_spawn_session` / `W1_spawn_herd` | Spawn a session from herd (job + window known, UUID/pid not). `status_source='reconcile'` is a white lie — the CHECK has no `'spawn'`; real provenance is `herd_sessions.source`. *(spawn path not yet wired to a CLI verb.)* |
| `W2_adopt` | SessionStart adopts herd's placeholder row for this window, joining on `(socket, window_id)` from env. Writes Claude's signals into the core row. Idempotent (`AND session_id IS NULL`). |
| `W2b_insert` | Fallback when W2 matched nothing: upsert on Claude's UUID. Resume revives a stopped row (`stopped_at=NULL`, fresh pid); `started_at` preserved so duration is total age. |
| `W2c_pid_claim` | Runs first, own txn: reap any stale live holder of this pid so `idx_sessions_pid_live` is satisfiable. Excludes our own row. NULL pid → no-op. |
| `W2b_placement` | Tier-2 half of the W2b fallback: record the kitty window the hook stands in, so a user-started `claude` is a first-class tracked session. Only writer of `source='hook'`. pk via `SELECT session_id`, not `last_insert_rowid()` (which returns the INSERTed, not the ON-CONFLICT-updated, row). |
| `W3d_reap` | Reaper: mark one session stopped whose pid the daemon found dead. `status_source='pid'` (inferred). |
| `W3e_boot_sweep` | Boot sweep (see [pid](#pid)). |
| `W4_event` / `W4_event_log` | Lifecycle: status + `last_event_*` in one statement, plus the append-only event. **No status guard** (see [two clocks](#two-clocks)). |
| `W4_end` | The only hook-driven death (`status_source='hook'` — it *knows*, vs W3d's inference). |
| `W5_statusline` | Metrics sink, UPDATE only (never creates a row — would resurrect stopped sessions / invent empty cwd). Never touches `last_event_*`. Resets_at arrives as unix epoch, converted to ISO in SQLite (zero date forks); `COALESCE` keeps prior value on NULL. The `prev_cost` pair (burn-rate delta, resampled >300s) is correct as written: an UPDATE's RHS sees the OLD row, so `prev_cost_usd` captures the previous total before this statement overwrites it. |
| `W5b_adopt` | Statusline adoption ("Path C"): statusline is a child of claude, inherits `KITTY_*` like a hook, so a reconciled session picks up metrics with no hooks wired. Same liveness JOIN as W2. |
| `W6a_arm` / `W6b_paged` / `W6c_ack` / `W6d_rearm` / `W6d_rearm_sid` | Attention (see [attention](#attention)). `W6d_rearm_sid` is the UUID-keyed variant a hook needs (hooks lack the surrogate pk) — same keyed-two-ways pattern as W2 vs W2b. |
| `R1_list` | The TUI's main read: all four tables, attention-first ordering. |
| `R_job_live` | Spawn-time recyclable-handle check: does a *live* session already hold this job name? By JOIN, no unique index. *(unused until the spawn verb lands.)* |
| `R_statusline` | Render input: feeds only the burn rate (the `prev_cost` pair). One read per fingerprint miss. |

**Routing read, not data read:** in an adopt writer (W2, W5b, W2b_placement) a
`herd_*` reference may appear only *after* `WHERE` (to pick which row), never in
the SET list. No tier-2 value ever enters a core column. `the test suite` enforces
this structurally.

The liveness JOIN inside the adopt subquery (`s.stopped_at IS NULL`) is
load-bearing: a window outlives the claude in it, so a dead predecessor row still
owns the `(socket, window_id)`. Without the filter, adoption binds Claude's UUID
to the stopped row while the live session stays unadopted and invisible.

**`herd_sessions` mutability contract:** `job_name`, `created_at` are immutable
(set once at spawn); `kitty_socket`, `window_id`, `herd_var`, `source`,
`verified_at` may be rewritten by a hook re-fire (e.g. resume in a new window).
Enforced by discipline — name columns in the UPDATE, never blanket-overwrite.
`W2b_placement` omits `job_name`/`created_at` and keeps `source='spawn'` so a
resumed spawned session never loses its job identity (`the test suite` section D).

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
redirect the program's state and end up testing the machine. (`HERD_RUNTIME`
earned this the hard way — written once as `${XDG_RUNTIME_DIR:-/tmp}`, it ignored
the test's override and wrote throttle state into the real `/run`, so a check
passed on first run and failed on every run after.)

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
  without it a hook fails outright the moment the daemon/TUI holds the write lock.
- **`stmt()`** extracts one `-- :name X` block, stopping at the first `;`.
  Stopping there also matters because prose after a statement contains things
  like `:pid MUST be ...` that `bind()` would try to substitute. An awk fork
  (0.7ms) beats the pure-bash equivalent (1.6ms) — measured.
- **`bind()`** expands `:name` params from `HERD_P_<name>` env vars in a **single
  pass**. Not sqlite3's `.param set`: its dot-command parser uses shell-like
  quoting, so a correct SQL escape (`o''brien`) mis-tokenizes, the param is left
  unbound, and sqlite3 silently binds NULL — silent data loss. Single-pass
  because sequential `${sql//:name/value}` rescans its own output (a cwd
  containing the literal `:now` gets mangled by the next substitution). Values
  travel via the environment, so no shell quoting is involved. Empty value →
  `NULL`; unknown param → hard failure (nonzero exit), never a silent NULL.
- **`run_tx`** wraps N statements in one `BEGIN IMMEDIATE ... COMMIT`, one fork,
  one WAL commit — halves sqlite3 spawns on the hot path and makes an event + its
  status change atomic. `BEGIN IMMEDIATE`, never plain `BEGIN`: a deferred txn
  upgrades to a write lock lazily and can throw `SQLITE_BUSY_SNAPSHOT` which the
  busy timeout cannot retry away. All binding happens before any SQL runs, so an
  unbound param aborts with nothing executed.
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
  invisible. `raw_json` is NULL by contract (events table is unbounded).
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

## Focus / jump (`kitty/focus.py`, `cli.py`)

Placement (`kitty_socket`, `window_id`) is a **cache, not a fact**. Before every
jump, `focus_session` re-derives the window from `kitten @ ls` and confirms the
window's foreground claude carries the session's stored pid — so it never focuses
a window reused after the session's window closed. Falls back to the stored
`window_id` when the pid can't be located, and self-heals the stored value when
it has drifted. A kitty restart invalidates every `window_id`; re-derivation
makes that invisible rather than fatal.

kitty's `--match pid:N` matches the *window's* pid (the login shell), never the
foreground claude — measured. So resolve pid → window ourselves and focus by
`--match id:`. `(socket, window_id)` is the whole jump key: `focus-window --match
id:N` on a window in a background tab activates that tab and returns 0, so no
`tab_id`/`os_window_id` is needed.

User vars (`--var HERD_JOB=x`, matched `--match var:HERD_JOB=^x$`) are
window-scoped, sticky, and survive claude exiting — they identify a *window*,
never a session, so always AND with pid liveness before believing a match. Match
values are unanchored regex — anchor them or `job` matches `job-2`.

IO (`_ls`/`_focus`, `list_fn`/`focus_fn`) is injected so logic is testable
without a live kitty — the same discipline as `daemon.py` and `common.sh`.

There is exactly one live-session read: `R1_list`, loaded from `writes.sql` like
every other shipping statement. `cli._live()` *is* that statement, and `ls`, the
picker, `rows` and the preview pane all go through it — `preview` filters it by
id in Python rather than issuing a second query. It was briefly two hand-written
transcriptions instead, which is precisely the rot `load_statements()` exists to
prevent: `R1_list` sat unused while the CLI's private copy missed the
`idx_sessions_live` plan the query test had been guarding all along.

`cli` surface: `ls`, `jump` and `watch` are the user verbs; `preview` (fzf's
per-highlight pane), `complete` (tab-completion feed), `rows` (fzf's reload
source) and `poke` (watch's refresh child) are machinery — callable, hidden from
help/completion. `jump` focuses immediately on a unique match (scriptable), else
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
first line, because **every other exit collapses into Esc's outcome**: `abort`
exits 130 with empty stdout, so a `ctrl-q:abort` bind is indistinguishable from a
cancel and `watch` just loops. That shipped broken. ctrl-c is worse than it looks
— fzf puts the terminal in raw mode, which disables ISIG, so ctrl-c never becomes
a SIGINT; fzf reads the raw `0x03` itself. `cmd_watch` still catches
`KeyboardInterrupt`, but that only covers the "no live sessions" sleep, where no
fzf is running and the terminal is in normal mode. `--expect` is the only route
for ctrl-c here — 0.44.1 has no `print(...)` action.

*The poker.* Only the row list needs refreshing, and fzf cannot refresh it on a
timer, so `cmd_poke` runs alongside and POSTs `reload` to fzf's `--listen` port.
It is stdlib `urllib`, not `curl` — herd ships no runtime deps, and the poker is
long-lived so there is no cold start to pay.

Three things about the poker were measured, not assumed:

- **`--listen=0` is not usable here.** fzf's `start` event does not reliably see
  `$FZF_PORT`, so a `start:execute-silent(… poke &)` bind spawned the poker on one
  picker and not the next — auto-refresh worked intermittently. `watch` picks the
  port itself (`_free_port`), which also makes it the poker's parent, so it can
  reap it in a `finally` instead of orphaning one per jump.
- **Reload only on change.** An unconditional 2s reload redraws the pane for
  nothing; the poker diffs the row text first.
- **But contact fzf every tick anyway** (`data=None` → a liveness GET). A poker
  that only spoke on change could not notice its fzf had exited while the herd was
  quiet. The first failures are tolerated (`_POKE_GRACE`): `watch` spawns the poker
  before fzf has bound the port, and treating that startup window as death killed
  auto-refresh outright.

---

## Attention {#attention}

"Needs attention" is **derived every tick** from a session's status + how long
it's been in it (`now - last_event_at`), never stored as a flag. What persists in
`herd_attention` is the **action/edge**, not the opinion — because paging is a
side effect in the world, and without memory a 1s poll would page you 60×/min
about one stuck session.

Page-worthy statuses and their grace (seconds, env-overridable):

- `waiting` (30s) — turn ended, Claude wants input.
- `needs_approval` (15s) — a permission prompt is up.
- `working` (300s) — no new event in a long time → likely stuck.

Statuses not listed (`stopped`, `unknown`) are never page-worthy.

- `attention_at` is the **edge**: when the rule first tripped (not the same as
  `last_event_at`'s age). `W6a_arm` uses `COALESCE` to preserve it across ticks.
- `W6c_ack` (implicit via focus, or explicit dismiss) guards on `attention_at <=
  focus_started_at`, closing a race where a hook raising a *new* attention
  mid-jump would be acked unseen.
- `W6d_rearm` deletes the whole row — a `DELETE` is a meaningful "shut up and
  re-evaluate everything". Kept in its own table (not columns on `herd_sessions`)
  because it's the only thing written on every tick, and isolating that write
  keeps contention off the read-mostly rows.

Actually *notifying* you (notify-send / TUI badge / escalation via `paged_level`)
is a separate actuator, deliberately deferred — this layer maintains the signal.

---

## Testing

The `pytest` suite under `tests/` is the only CI gate: import-linter can't see
the tier boundary because that boundary is SQL and the hooks are bash. It applies
the real schema, loads the real statements through `herd.db`, and execs the
**real** hooks (directly, not via `bash <path>` — a missing `+x` must fail the
same way production does) against a per-test throwaway DB.

    python3 -m pytest              # whole suite (~1s)
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

- `herd new` spawn verb (SQL: `W1_spawn_*`, `R_job_live` — written, unwired).
- The notifier/pager actuator (`W6b_paged`, `paged_level` escalation).
- A `pid_start_time` column to close the reboot pid-reuse caveat properly.
