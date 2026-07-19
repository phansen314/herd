-- ═══════════════════════════════════════════════════════════════════════════
-- herd — canonical write paths. The ONLY statements that write.
-- Loaded by herd.db.load_statements() (python) AND common.sh stmt() (bash);
-- both cut at the first ';'; test_hooks.py::test_bash_and_python_extract_same asserts
-- they agree (whitespace-normalized).
-- Rationale + a per-statement table: DESIGN.md#write-paths-schemawritessql.
-- Inline comments inside a statement must not contain ';'.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── W1. SPAWN (herd/kitty) — TWO PHASE, and the order is load-bearing.
-- Phase 1 (RESERVE, inside BEGIN IMMEDIATE with the R_job_live re-check): claim the
-- job name with window_id NULL, BEFORE the kitty launch. Phase 2 (W1_spawn_window):
-- stamp the window once kitten @ launch returns.
-- Reserving first is what makes the live-job check atomic: the launch is a subprocess
-- + socket round trip, and checking across it let two concurrent spawns both pass.
-- No unique index can back this up — job_name must repeat across dead rows, and a
-- partial "live" index would have to reach into sessions.stopped_at. See
-- DESIGN.md#liveness. window_id is MUTABLE by contract (herd.sql), and a NULL
-- placement is already a handled state (focus.py: "no window to focus").
-- status_source='reconcile' is a white lie (CHECK has no 'spawn'); real
-- provenance is herd_sessions.source. Driven by `herd spawn` (cli.cmd_spawn).
-- :name W1_spawn_session
INSERT INTO sessions(cwd, status, status_source, started_at, updated_at)
VALUES(:cwd, 'unknown', 'reconcile', :now, :now);
-- :name W1_spawn_herd
INSERT INTO herd_sessions(session_pk, job_name, created_at,
                          kitty_socket, window_id,
                          herd_var, source, verified_at)
VALUES(:pk, :job, :now, :socket, NULL, :job, 'spawn', :now);

-- W1b: the launch returned — stamp the placement onto the reservation.
-- :name W1_spawn_window
UPDATE herd_sessions SET window_id = :win, verified_at = :now WHERE session_pk = :pk;

-- W1c: the launch FAILED — drop the reservation so the job name frees immediately.
-- DELETE, not stopped_at: this session never existed, so it must not appear in
-- history. sessions.id is AUTOINCREMENT precisely so a real DELETE never causes
-- surrogate reuse; the herd_sessions row falls away via ON DELETE CASCADE.
-- :name W1_spawn_abort
DELETE FROM sessions WHERE id = :pk;


-- ── W2. ADOPT via window_id (session_start.sh). Joins on (socket, window_id)
-- from env; idempotent (AND session_id IS NULL). The subquery's stopped_at IS
-- NULL is load-bearing — a dead predecessor still owns this window. Routing read
-- only: herd_ appears after WHERE, never in SET. See DESIGN.md#write-paths-schemawritessql.
-- :name W2_adopt
UPDATE sessions
SET session_id      = :session_id,
    cwd             = :cwd,
    model           = :model,
    transcript_path = :transcript,
    pid             = :pid,          -- claude's own pid (W2c_pid_claim freed any stale holder)
    status          = 'working',
    status_source   = 'hook',
    last_event_at   = :now,
    last_event_type = 'start',
    updated_at      = :now
WHERE id = (SELECT h.session_pk FROM herd_sessions h
            JOIN sessions s ON s.id = h.session_pk
            WHERE h.kitty_socket = :socket AND h.window_id = :win
              AND s.stopped_at IS NULL)
  AND session_id IS NULL
  AND stopped_at IS NULL;

-- W2b. Fallback when W2 matched nothing: upsert on Claude's own key. Resume
-- revives a stopped row (stopped_at=NULL, fresh pid); started_at preserved so
-- duration is total age.
-- :name W2b_insert
INSERT INTO sessions(session_id, cwd, model, transcript_path, pid, status,
                     status_source, last_event_at, last_event_type,
                     started_at, updated_at)
VALUES(:session_id, :cwd, :model, :transcript, :pid, 'working', 'hook',
       :now, 'start', :now, :now)
ON CONFLICT(session_id) DO UPDATE SET
    cwd             = excluded.cwd,
    transcript_path = excluded.transcript_path,
    model           = COALESCE(excluded.model, sessions.model),
    status          = 'working',
    stopped_at      = NULL,          -- resume revives a stopped session
    pid             = excluded.pid,  -- with the resumed process's own pid
    last_event_at   = excluded.last_event_at,
    last_event_type = excluded.last_event_type,
    updated_at      = excluded.updated_at;

-- W2c. Runs at SessionStart BEFORE the pid is written, own txn: reap any OTHER
-- live row holding this pid (provably stale — the hook is a descendant of the
-- claude that owns :pid now) so idx_sessions_pid_live is satisfiable. Excludes
-- our own row; NULL pid -> no-op. See DESIGN.md#pid.
-- :name W2c_pid_claim
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE pid = :pid AND stopped_at IS NULL
  AND (session_id IS NULL OR session_id IS NOT :session_id);

-- W2b_placement. Tier-2 half of the W2b fallback: record the window the hook
-- stands in, so a user-started `claude` is first-class. Only writer of
-- source='hook'. pk via SELECT on session_id (NOT last_insert_rowid(), which
-- returns the INSERTed not the ON-CONFLICT-updated row). Run in the SAME run_tx
-- as W2b_insert. Omits job_name/created_at + keeps source unchanged per the
-- mutability contract. Trailing WHERE = no-op suppressor for an unchanged re-fire.
-- :name W2b_placement
INSERT INTO herd_sessions(session_pk, kitty_socket, window_id, source, verified_at)
SELECT id, :socket, :win, 'hook', :now FROM sessions WHERE session_id = :session_id
ON CONFLICT(session_pk) DO UPDATE SET
    kitty_socket = excluded.kitty_socket,
    window_id    = excluded.window_id,
    verified_at  = excluded.verified_at
WHERE herd_sessions.kitty_socket IS NOT excluded.kitty_socket
   OR herd_sessions.window_id    IS NOT excluded.window_id;


-- ── W3. LIVENESS REAPER (daemon.py). Silent-death detection: a -9/crash/closed
-- terminal fires no SessionEnd, so the row would sit live forever. Liveness from
-- the PROCESS TABLE, never kitty. See DESIGN.md#liveness.

-- W3d: reap one session the daemon found dead. status_source='pid' (inferred).
-- :pid is NOT redundant with the caller's SELECT — it is what makes this statement
-- self-validating. reap_once reads (id, pid), forks `ps` (up to PS_TIMEOUT), then
-- writes. A resume landing in that window sets a NEW pid and clears stopped_at, and
-- keyed on id alone this reaped a live process it had never observed. Re-asserting
-- the pid the decision was made about turns the race into a 0-row no-op. Every other
-- statement acting on a prior read (W3f, W2c_pid_claim, W2_adopt, W6c_ack) does the
-- same. This one was the outlier.
-- :name W3d_reap
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE id = :pk AND stopped_at IS NULL AND pid = :pid;

-- W3f: STRANDED RESERVATION SWEEP. A phase-1 spawn reservation whose claude never
-- reached SessionStart: the launcher raised, claude died before its first hook, or
-- the W5b adoption lost the session_id race. Such a row is pid NULL + session_id
-- NULL, so W3d skips it forever (pid IS NOT NULL) while R_job_live still counts it
-- live — the job name stays burned until W3e's next boot sweep. Age-gated because a
-- reservation is legitimately pid-NULL for the span of the launch round trip.
-- DELETE, not stopped_at, for W1_spawn_abort's reason: this session never existed,
-- so it must not appear in history. CASCADE takes the herd_ rows.
-- :name W3f_sweep_stranded
DELETE FROM sessions
WHERE stopped_at IS NULL AND pid IS NULL AND session_id IS NULL
  AND started_at < :cutoff;

-- W3e: BOOT SWEEP (once at startup). After a reboot, pids may be recycled, so
-- W3d's liveness check can read a dead session as alive. See DESIGN.md#pid.
-- started_at ALONE IS NOT EVIDENCE OF DEATH. W2b_insert's ON CONFLICT branch
-- deliberately preserves started_at (so duration is total age) while setting a
-- fresh pid and clearing stopped_at, so a RESUMED session carries a pre-boot
-- started_at with a live post-boot process. Sweeping on started_at alone reaped it
-- — and because boot_time is fixed, re-reaped it on every subsequent daemon start,
-- undoing any manual recovery.
-- last_event_at is the honest liveness signal here. Every hook advances it, so a
-- session that has done anything since boot is spared, and a genuine pre-boot
-- corpse (last_event_at older than boot, or never set) is still swept.
-- :name W3e_boot_sweep
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE stopped_at IS NULL AND started_at < :boot_time
  AND (last_event_at IS NULL OR last_event_at < :boot_time);


-- ── W4. LIFECYCLE HOOKS. The single lifecycle write: status + last_event_* on
-- one row. NO `AND status IS NOT :status` GUARD, EVER — it freezes last_event_at
-- on the hot path (post_tool_use always 'working'), making a busy session read
-- silent and paging you about it. sessions.last_event_at is the ONLY event signal
-- anything reads — there is no second (events-log) write to keep in sync anymore.
-- See DESIGN.md#two-clocks.
-- The stopped_at guard is what every other live-row write already carries (W4_end,
-- W5_statusline, W2_adopt, W5b_adopt). Its absence here was the reason a wrongly
-- reaped session could never recover. The hooks keep firing, but this statement
-- cannot clear stopped_at, and R1_list filters on it — so the session stayed
-- invisible for the rest of its life instead of healing on the next tool call. It
-- also produced rows that were status='working' AND stopped, a combination the
-- CHECK permits and no reader expects.
-- NOT the forbidden `status IS NOT :status` guard — that one would break the
-- attention timer. Liveness is a different predicate.
-- :name W4_event
UPDATE sessions
SET status          = :status,
    status_source   = 'hook',
    last_event_at   = :now,
    last_event_type = :etype,
    updated_at      = :now
WHERE session_id = :session_id AND stopped_at IS NULL;

-- W4b: SESSION END — the only hook-driven death. status_source='hook' (it KNOWS,
-- vs W3d's inference). session_end.sh MUST be registered BLOCKING.
-- :name W4_end
UPDATE sessions
SET status          = 'stopped',
    status_source   = 'hook',
    stopped_at      = :now,
    last_event_at   = :now,
    last_event_type = 'end',
    updated_at      = :now
WHERE session_id = :session_id AND stopped_at IS NULL;


-- ── W5. STATUSLINE — the sink for EVERY field the statusLine payload carries
-- (metrics, version, output_style, worktree, original_cwd), plus git_branch, which
-- the hook derives from a pure-bash .git walk. UPDATE ONLY (never creates a row:
-- would resurrect stopped sessions / invent empty cwd).
-- NEVER touches last_event_*. Fires ~1/sec, guarded
-- upstream by the fingerprint diff-cache. resets_at arrives as unix epoch,
-- converted to ISO in SQLite; COALESCE keeps the prior value on NULL. The
-- prev_cost pair captures the OLD row's total (an UPDATE's RHS sees the old row) —
-- correct as written, don't "fix" it. See DESIGN.md#write-paths-schemawritessql.
-- :name W5_statusline
UPDATE sessions SET
    model                = COALESCE(:model, model),
    session_name         = COALESCE(:sname, session_name),
    context_percent      = COALESCE(CAST(:ctx AS INTEGER), context_percent),
    total_cost_usd       = COALESCE(:cost, total_cost_usd),
    git_branch           = COALESCE(:branch, git_branch),
    git_worktree         = COALESCE(:gwt, git_worktree),
    original_cwd         = COALESCE(:ocwd, original_cwd),
    claude_code_version  = COALESCE(:ver, claude_code_version),
    output_style         = COALESCE(:ostyle, output_style),
    context_window_size  = COALESCE(CAST(:ctxsize AS INTEGER), context_window_size),
    exceeds_200k_tokens  = COALESCE(CAST(:exc200 AS INTEGER), exceeds_200k_tokens),
    total_input_tokens   = COALESCE(CAST(:tokin AS INTEGER), total_input_tokens),
    total_output_tokens  = COALESCE(CAST(:tokout AS INTEGER), total_output_tokens),
    lines_added          = COALESCE(CAST(:ladd AS INTEGER), lines_added),
    lines_removed        = COALESCE(CAST(:ldel AS INTEGER), lines_removed),
    api_duration_ms      = COALESCE(CAST(:apims AS INTEGER), api_duration_ms),
    rate_limit_5h_percent   = COALESCE(:rl5, rate_limit_5h_percent),
    rate_limit_5h_resets_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', :rl5reset, 'unixepoch'),
                                       rate_limit_5h_resets_at),
    rate_limit_7d_percent   = COALESCE(:rl7, rate_limit_7d_percent),
    rate_limit_7d_resets_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', :rl7reset, 'unixepoch'),
                                       rate_limit_7d_resets_at),
    prev_cost_usd        = CASE WHEN prev_cost_sampled_at IS NULL
                             OR (strftime('%s','now')
                                 - strftime('%s', prev_cost_sampled_at)) > 300
                            THEN total_cost_usd ELSE prev_cost_usd END,
    prev_cost_sampled_at = CASE WHEN prev_cost_sampled_at IS NULL
                             OR (strftime('%s','now')
                                 - strftime('%s', prev_cost_sampled_at)) > 300
                            THEN updated_at ELSE prev_cost_sampled_at END,
    updated_at           = :now       -- NOT last_event_at
WHERE session_id = :session_id AND stopped_at IS NULL;

-- W5b: statusline ADOPTION (Path C). statusline is a child of claude, inherits
-- $KITTY_* like a hook, so a reconciled session picks up metrics with no hooks
-- wired. Same liveness JOIN as W2. Still UPDATE only.
-- :name W5b_adopt
UPDATE sessions
SET session_id = :session_id, updated_at = :now
WHERE id = (SELECT h.session_pk FROM herd_sessions h
            JOIN sessions s ON s.id = h.session_pk
            WHERE h.kitty_socket = :socket AND h.window_id = :win
              AND s.stopped_at IS NULL)
  AND session_id IS NULL
  AND stopped_at IS NULL;


-- ── W6. ATTENTION (daemon.py). "Needs attention" is derived each tick; only the
-- action/edge persists here. See DESIGN.md#attention.
-- W6a: arm — the rule tripped. COALESCE preserves the edge across ticks.
-- :name W6a_arm
INSERT INTO herd_attention(session_pk, attention_at)
VALUES(:pk, :now)
ON CONFLICT(session_pk) DO UPDATE SET attention_at = COALESCE(attention_at, :now);

-- (no W6b: there is no page action to record. herd owns no actuator, so the
-- signal is binary — armed or acked. See DECISIONS.md.)

-- W6c: ack — written by focus_session(), the only caller. The attention_at<=focus_started_at
-- guard closes a race where a NEW attention raised mid-jump would be acked unseen.
-- :name W6c_ack
UPDATE herd_attention SET ack_at = :now
WHERE session_pk = :pk
  AND ack_at IS NULL
  AND attention_at IS NOT NULL
  AND attention_at <= :focus_started_at;

-- W6d: RE-ARM. A new semantic event clears the row so the rule may trip fresh —
-- this is what makes ack mean "I've seen THIS silence".
-- :name W6d_rearm
DELETE FROM herd_attention WHERE session_pk = :pk;

-- W6d_sid: same re-arm keyed by Claude's UUID (a hook lacks the surrogate pk).
-- Keeping it here (not inlined in stop.sh) preserves the single-write-path guard.
-- :name W6d_rearm_sid
DELETE FROM herd_attention
WHERE session_pk = (SELECT id FROM sessions WHERE session_id = :session_id);

-- W6e: DROP ATTENTION FOR THE DEAD. Death is recorded by UPDATE (W3d_reap, W4_end),
-- never DELETE, so the ON DELETE CASCADE never fires; and attention_tick only
-- visits rows WHERE stopped_at IS NULL, so a session that dies while armed keeps
-- its herd_attention row forever. Invisible to every read (R1_list joins through
-- the same liveness filter) and therefore unbounded — one orphan per session that
-- ever needed you and then died.
-- :name W6e_sweep_dead
DELETE FROM herd_attention
WHERE session_pk IN (SELECT id FROM sessions WHERE stopped_at IS NOT NULL);


-- ── R_statusline. RENDER INPUT (statusline.sh): feeds only the burn rate (the
-- prev_cost pair). One read per fingerprint miss. '|'-joined for a single bash read.
-- :name R_statusline
SELECT COALESCE(s.prev_cost_usd,'') || '|' || COALESCE(s.prev_cost_sampled_at,'')
FROM sessions s WHERE s.session_id = :session_id;


-- ── R_job_live. Recyclable-handle check (spawn): does a LIVE session already
-- hold this job name? By JOIN, no unique index. Called by spawn.spawn() BEFORE
-- the launch, so a rejection never opens a tab. Dead rows keep their job_name —
-- name reuse is by design, and resolution only ever searches live sessions.
-- :name R_job_live
SELECT h.session_pk FROM herd_sessions h
JOIN sessions s ON s.id = h.session_pk
WHERE h.job_name = :job AND s.stopped_at IS NULL;

-- Is there an unadopted reservation in this window — i.e. something for statusline
-- Path C to adopt later? Exactly W2_adopt's target predicate, as a read.
--
-- session_start.sh needs this to tell two very different situations apart after a
-- FAILED W2_adopt: a spawn reservation waiting to be claimed (deferring is right,
-- Path C will get it) versus nothing at all, which is every user-started claude
-- (deferring loses the session permanently — Path C only ever UPDATEs, so with no
-- row there is nothing to adopt and SessionStart never fires again).
--
-- A read answers even while the write lock is held: WAL readers do not block on a
-- writer. So this is reliable in precisely the case that made W2_adopt fail.
-- :name R_window_unadopted
SELECT 1 FROM herd_sessions h
JOIN sessions s ON s.id = h.session_pk
WHERE h.kitty_socket = :socket AND h.window_id = :win
  AND s.stopped_at IS NULL AND s.session_id IS NULL;


-- ── R1. The ONE live-session read: sessions + herd_sessions + herd_attention,
-- attention-first ordering. ls, the picker, rows and preview all go through it.
-- SELECT ONLY WHAT A RENDERER CONSUMES. h.herd_var, h.source and h.verified_at
-- were selected here and read by nothing — paid for on every `herd ls` and every
-- `watch` refresh. They are still WRITTEN, and the mutability contract in
-- DESIGN.md#write-paths-schemawritessql still governs them; this read just stopped
-- carrying them. Add a column back when a caller actually reads it.
-- :name R1_list
SELECT s.id, s.session_id, s.pid, s.cwd, s.status, s.status_source, s.model, s.session_name,
       s.context_percent, s.total_cost_usd, s.git_branch,
       s.last_event_at, s.last_event_type, s.started_at, s.updated_at,
       h.job_name, h.kitty_socket, h.window_id,
       a.attention_at, a.ack_at
FROM sessions s
LEFT JOIN herd_sessions  h ON h.session_pk = s.id
LEFT JOIN herd_attention a ON a.session_pk = s.id
WHERE s.stopped_at IS NULL
-- s.id DESC makes the order TOTAL: started_at is second-resolution, so a scripted
-- multi-spawn ties and the list reorders between renders of `herd watch`.
ORDER BY a.attention_at IS NULL,   -- attention first
         a.attention_at ASC,       -- longest-waiting first
         s.started_at DESC,
         s.id DESC;
