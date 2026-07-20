-- ═══════════════════════════════════════════════════════════════════════════
-- herd — canonical write paths. The ONLY statements that write.
-- Loaded by herd.db.load_statements() (python) AND common.sh stmt() (bash);
-- both cut at the first ';'; test_hooks.py::test_bash_and_python_extract_same asserts
-- they agree (whitespace-normalized).
-- Rationale + a per-statement table: DESIGN.md#write-paths-schemawritessql.
-- Inline comments inside a statement must not contain ';'.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── W1. SPAWN — TWO PHASE, order load-bearing. Phase 1 RESERVE (inside BEGIN
-- IMMEDIATE + R_job_live re-check) claims the job name with window_id NULL BEFORE
-- the kitty launch, which is what makes the live-job check atomic: the launch is a
-- subprocess + socket round trip, and checking across it let two concurrent spawns
-- both pass. No unique index can back this — job_name repeats across dead rows.
-- status_source='reconcile' is a white lie (the CHECK has no 'spawn').
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

-- W1c: the launch FAILED — free the job name. DELETE, not stopped_at: this session
-- never existed. sessions.id is AUTOINCREMENT precisely so a real DELETE never
-- reuses a surrogate. The herd_sessions row goes via ON DELETE CASCADE.
-- :name W1_spawn_abort
DELETE FROM sessions WHERE id = :pk;


-- ── W2. ADOPT via window_id (session_start.sh). Idempotent via session_id IS NULL.
-- The subquery's stopped_at IS NULL is load-bearing — a dead predecessor still owns
-- this window. Routing read only: herd_ after WHERE, never in SET.
-- ORDER BY ASC LIMIT 1: the DB permits two live rows in one window
-- (test_window_reuse), so the bare subquery was plan-dependent. ASC, NOT DESC —
-- DESC returns a newer unadopted reservation, the outer session_id IS NULL passes,
-- and a re-fire stamps an already-taken session_id onto it (UNIQUE violation). ASC
-- returns the adopted row, so the hook correctly falls through to W2b_insert.
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
              AND s.stopped_at IS NULL
            ORDER BY h.session_pk ASC LIMIT 1)
  AND session_id IS NULL
  AND stopped_at IS NULL;

-- W2_adopt_job. SECOND adoption route, by job identity: the spawned claude carries
-- HERD_JOB in its ENVIRONMENT (launch.py --env), so the hook need not consult the
-- window stamp — which is not reliably there yet, since W1_spawn_window takes the
-- WAL write lock (up to a 3s busy_timeout) while claude is already starting.
-- ORDER BY DESC LIMIT 1 because job_name is not unique across dead rows and a resume
-- can leave two live claimants (DECISIONS.md#clear-inherits-job) — newest wins.
-- :name W2_adopt_job
UPDATE sessions
SET session_id      = :session_id,
    cwd             = :cwd,
    model           = :model,
    transcript_path = :transcript,
    pid             = :pid,
    status          = 'working',
    status_source   = 'hook',
    last_event_at   = :now,
    last_event_type = 'start',
    updated_at      = :now
WHERE id = (SELECT h.session_pk FROM herd_sessions h
            JOIN sessions s ON s.id = h.session_pk
            WHERE h.job_name = :job AND s.stopped_at IS NULL AND s.session_id IS NULL
            ORDER BY h.session_pk DESC LIMIT 1)
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

-- W2c. Runs at SessionStart BEFORE the pid is written, own txn: reap any OTHER live
-- row holding this pid (provably stale — the hook is a descendant of the claude that
-- owns :pid now) so idx_sessions_pid_live is satisfiable. NULL pid -> no-op.
-- :name W2c_pid_claim
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE pid = :pid AND stopped_at IS NULL
  AND (session_id IS NULL OR session_id IS NOT :session_id);

-- W2b_placement. Tier-2 half of the W2b fallback: record the window the hook stands
-- in, so a user-started `claude` is first-class. Only writer of source='hook'. pk via
-- SELECT on session_id (NOT last_insert_rowid(), which returns the INSERTed not the
-- ON-CONFLICT-updated row). Run in the SAME run_tx as W2b_insert. Leaves source and
-- created_at alone per the mutability contract. Trailing WHERE suppresses no-op re-fires.
-- job_name is INHERITED on the INSERT branch and only there, so a spawned job survives
-- /clear (a NEW session_id in the SAME window that W2_adopt cannot match, its subquery
-- needing stopped_at IS NULL). Not a mutation — job_name is immutable ONCE SET.
-- THE PID IS THE DISCRIMINATOR, not a time window: /clear does not restart the process,
-- so same pid + same window is the same tab continuing. A recycled window holds a
-- different pid, and a NULL pid matches nothing.
-- :name W2b_placement
INSERT INTO herd_sessions(session_pk, job_name, kitty_socket, window_id, source, verified_at)
SELECT s.id,
       -- :job (HERD_JOB from the env) WINS over inference: it is the only thing that
       -- survives the reservation being deleted outright — a claude held at the trust
       -- prompt past HERD_STRANDED_SECS is swept by W3f before SessionStart fires.
       COALESCE(:job,
       (SELECT h2.job_name FROM herd_sessions h2
          JOIN sessions s2 ON s2.id = h2.session_pk
         WHERE h2.kitty_socket = :socket AND h2.window_id = :win
           AND s2.stopped_at IS NOT NULL
           AND s2.pid IS NOT NULL AND s2.pid = s.pid
           AND h2.job_name IS NOT NULL
         ORDER BY s2.stopped_at DESC, s2.id DESC LIMIT 1)),
       :socket, :win, 'hook', :now
FROM sessions s WHERE s.session_id = :session_id
ON CONFLICT(session_pk) DO UPDATE SET
    kitty_socket = excluded.kitty_socket,
    window_id    = excluded.window_id,
    verified_at  = excluded.verified_at
WHERE herd_sessions.kitty_socket IS NOT excluded.kitty_socket
   OR herd_sessions.window_id    IS NOT excluded.window_id;


-- ── W3. LIVENESS REAPER (daemon.py). A -9/crash/closed terminal fires no SessionEnd,
-- so the row would sit live forever. Liveness from the PROCESS TABLE, never kitty.

-- W3d: reap one session the daemon found dead. :pid is NOT redundant with the
-- caller's SELECT: reap_once reads (id, pid), forks `ps`, then writes, and a resume
-- in that window sets a NEW pid — keyed on id alone this reaped a live process.
-- Re-asserting the observed pid makes the race a 0-row no-op.
-- :name W3d_reap
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE id = :pk AND stopped_at IS NULL AND pid = :pid;

-- W3f: STRANDED RESERVATION SWEEP. A phase-1 reservation whose claude never reached
-- SessionStart is pid NULL + session_id NULL, so W3d skips it forever while R_job_live
-- still counts it live, burning the job name. Age-gated because a reservation is
-- legitimately pid-NULL across the launch round trip. DELETE for W1_spawn_abort's reason.
-- :name W3f_sweep_stranded
DELETE FROM sessions
WHERE stopped_at IS NULL AND pid IS NULL AND session_id IS NULL
  AND started_at < :cutoff;

-- W3e: BOOT SWEEP (once at startup). After a reboot pids may be recycled, so W3d can
-- read a dead session as alive. started_at ALONE IS NOT EVIDENCE OF DEATH: W2b_insert
-- preserves started_at while setting a fresh pid, so a RESUMED session has a pre-boot
-- started_at with a live process. last_event_at is the honest signal — every hook
-- advances it. See DESIGN.md#pid.
-- :name W3e_boot_sweep
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE stopped_at IS NULL AND started_at < :boot_time
  AND (last_event_at IS NULL OR last_event_at < :boot_time);


-- ── W4. LIFECYCLE HOOKS. The single lifecycle write. NO `AND status IS NOT :status`
-- GUARD, EVER — it freezes last_event_at on the hot path (post_tool_use is always
-- 'working'), making a busy session read silent and paging you about it.
-- sessions.last_event_at is the ONLY event signal anything reads. DESIGN.md#two-clocks.
-- The stopped_at guard is a different predicate and IS required — but not because it
-- restores a wrongly reaped session. That session never recovers either way: no hook
-- can clear stopped_at and R1_list filters on it. What the guard prevents is the row
-- LYING about itself. Without it a later hook sets status='working' on a stopped row,
-- giving status='working' AND stopped_at NOT NULL — which the CHECK permits and no
-- reader expects.
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


-- ── W5. STATUSLINE — the sink for EVERY field the statusLine payload carries, plus
-- git_branch. UPDATE ONLY (an INSERT would resurrect stopped sessions / invent an
-- empty cwd). NEVER touches last_event_*. Fires ~1/sec. The prev_cost pair captures
-- the OLD row's total (an UPDATE's RHS sees the old row) — correct as written.
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

-- W5b: statusline ADOPTION (Path C). statusline is a child of claude and inherits
-- $KITTY_* like a hook, so a reconciled session picks up metrics with no hooks wired.
-- :name W5b_adopt
UPDATE sessions
SET session_id = :session_id, updated_at = :now
WHERE id = (SELECT h.session_pk FROM herd_sessions h
            JOIN sessions s ON s.id = h.session_pk
            WHERE h.kitty_socket = :socket AND h.window_id = :win
              AND s.stopped_at IS NULL
            ORDER BY h.session_pk ASC LIMIT 1)
  AND session_id IS NULL
  AND stopped_at IS NULL;


-- ── W6. ATTENTION (daemon.py). "Needs attention" is derived each tick, only the edge
-- persists. See DESIGN.md#attention.
-- W6a: arm. COALESCE preserves the edge across ticks. The SELECT re-asserts the
-- caller's snapshot (attention_tick reads every live row then writes, and a hook
-- firing in between armed a just-active session). :cutoff is now minus the status
-- threshold, so "still silent" is re-checked at write time.
-- :name W6a_arm
INSERT INTO herd_attention(session_pk, attention_at)
SELECT id, :now FROM sessions
 WHERE id = :pk AND stopped_at IS NULL
   AND last_event_at IS NOT NULL AND last_event_at <= :cutoff
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
-- never DELETE, so ON DELETE CASCADE never fires, and attention_tick only visits
-- rows WHERE stopped_at IS NULL — so an armed row outlives its session, unbounded.
-- :name W6e_sweep_dead
DELETE FROM herd_attention
WHERE session_pk IN (SELECT id FROM sessions WHERE stopped_at IS NOT NULL);


-- ── R_statusline. RENDER INPUT (statusline.sh): feeds only the burn rate (the
-- prev_cost pair). One read per fingerprint miss. '|'-joined for a single bash read.
-- :name R_statusline
SELECT COALESCE(s.prev_cost_usd,'') || '|' || COALESCE(s.prev_cost_sampled_at,'')
FROM sessions s WHERE s.session_id = :session_id;


-- ── R_job_live. Recyclable-handle check (spawn): does a LIVE session already hold
-- this job name? By JOIN, no unique index. Called BEFORE the launch, so a rejection
-- never opens a tab. Dead rows keep their job_name — reuse is by design.
-- :name R_job_live
SELECT h.session_pk FROM herd_sessions h
JOIN sessions s ON s.id = h.session_pk
WHERE h.job_name = :job AND s.stopped_at IS NULL;

-- Is there an unadopted reservation in this window for Path C to adopt later?
-- W2_adopt's target predicate as a read. session_start.sh needs it to tell two cases
-- apart after a FAILED W2_adopt: a reservation waiting to be claimed (defer) versus
-- nothing at all, a user-started claude, where deferring loses the session for good
-- (Path C only UPDATEs). Works while the write lock is held — WAL readers never block.
-- :name R_window_unadopted
SELECT 1 FROM herd_sessions h
JOIN sessions s ON s.id = h.session_pk
WHERE h.kitty_socket = :socket AND h.window_id = :win
  AND s.stopped_at IS NULL AND s.session_id IS NULL;


-- ── R1. The ONE live-session read, attention-first. ls, the picker, rows and preview
-- all go through it. SELECT ONLY WHAT A RENDERER CONSUMES — h.herd_var, h.source and
-- h.verified_at are still WRITTEN, just unread. Add a column back when one is read.
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
