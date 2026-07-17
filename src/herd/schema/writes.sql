-- ═══════════════════════════════════════════════════════════════════════════
-- herd — canonical write paths. These are the ONLY statements that write.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── W1. SPAWN (herd/kitty) ────────────────────────────────────────────────
-- After `kitten @ launch --var HERD_JOB=<job> --tab-title <job> --cwd <cwd> claude`
-- returns a window_id. We know: job, window, tab, cwd. We do NOT know:
-- session_id (Claude hasn't told us) or pid (not yet).
--
-- herd launches TABS and PANES only, never OS windows. That is why the focus
-- path never has to ask a compositor to raise anything.
--
-- status_source='reconcile' is a white lie: the CHECK has no 'spawn' value.
-- Provenance lives in herd_sessions.source, which does. Don't read
-- status_source as provenance.
-- :name W1_spawn_session
INSERT INTO sessions(cwd, status, status_source, started_at, updated_at)
VALUES(:cwd, 'unknown', 'reconcile', :now, :now);
-- :name W1_spawn_herd
INSERT INTO herd_sessions(session_pk, job_name, created_at,
                          kitty_socket, window_id,
                          herd_var, source, verified_at)
VALUES(:pk, :job, :now, :socket, :win, :job, 'spawn', :now);


-- ── W2. ADOPT via window_id (core/session_start.sh) ───────────────────────
-- The hook runs INSIDE the kitty window, so $KITTY_WINDOW_ID is free from env
-- (klawde's kitty_start.sh does exactly this — zero forks). A kitty window has
-- exactly one LIVE claude in it, so (socket, window_id) + liveness is an exact
-- join key.
-- This sidesteps pid entirely for the spawn path: SPIKE-1 is NOT blocking here.
-- Idempotent: `AND session_id IS NULL` makes a re-fire a no-op.
--
-- The liveness JOIN (`s.stopped_at IS NULL` inside the subquery) IS LOAD-BEARING.
-- A window outlives the claude in it, so a DEAD predecessor row still owns this
-- window_id. Without the filter the subquery matches it and binds Claude's UUID
-- to the stopped row while the live session stays unadopted — R1 and W5 both
-- filter `stopped_at IS NULL`, so that session goes invisible and collects no
-- metrics, silently. Filtering by the session's own stopped_at (not a stored
-- herd flag) is the whole point of the decouple: one source of truth.
--
-- ROUTING READ, NOT A DATA READ. The herd_sessions subquery only selects WHICH
-- session row to adopt (the placeholder herd created at spawn). Every value
-- WRITTEN into the core row is a Claude signal (:session_id, :cwd, :model, …) —
-- no tier-2 value ever enters a core column. validate.py enforces this: in a
-- core writer, herd_ may appear only after WHERE (routing), never in the SET
-- list. Same for W3a_discover and W5b_adopt.
-- :name W2_adopt
UPDATE sessions
SET session_id      = :session_id,
    cwd             = :cwd,
    model           = :model,
    transcript_path = :transcript,
    pid             = :pid,          -- claude's own pid (claude_pid ppid-walk). The
                                     -- SessionStart hook is a live descendant of
                                     -- claude, so this is exact. W2c_pid_claim has
                                     -- already freed any stale holder of it.
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

-- W2b. Fallback when W2 matched nothing (no herd_sessions row: hooks wired but
-- herd never saw this window). Plain upsert on Claude's own key.
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
    pid             = excluded.pid,  -- ...with the RESUMED process's own pid. The
                                     -- SessionStart hook re-walks to claude on every
                                     -- (re)start, so the fresh pid replaces the dead
                                     -- old one directly (no null-and-wait). W2c_pid_
                                     -- claim already reaped any stale holder of it,
                                     -- so idx_sessions_pid_live cannot reject this.
    last_event_at   = excluded.last_event_at,
    last_event_type = excluded.last_event_type,
    updated_at      = excluded.updated_at;
    -- NOTE: started_at deliberately preserved. Resume = same session, fresh
    -- location; Duration reflects total age. (klawde's semantics — correct.)

-- W2c_pid_claim. Runs at SessionStart BEFORE the pid is written, in its own txn, so
-- idx_sessions_pid_live (one LIVE row per pid) is clear before W2_adopt/W2b_insert
-- stamp :pid. Reaps any OTHER live row still holding this pid — provably stale: the
-- hook runs AS a descendant of the claude that owns :pid RIGHT NOW, so a different
-- live row claiming it died silently (no SessionEnd) and its pid was recycled to us.
-- Without this, that stale row would make the new session's pid write fail the
-- UNIQUE index, and the error-swallowing hook would drop the session silently.
--
-- Excludes our own row (session_id match) so a resume doesn't reap itself. When the
-- walk failed, :pid binds NULL and `pid = NULL` matches nothing — a clean no-op.
-- status_source='pid' (death inferred from liveness, like W3d); NEVER last_event_*.
-- :name W2c_pid_claim
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE pid = :pid AND stopped_at IS NULL
  AND (session_id IS NULL OR session_id IS NOT :session_id);

-- W2b_placement. The tier-2 half of the W2b fallback. W2b_insert writes the
-- session row (tier 1); this records the kitty window the hook is STANDING IN, so
-- a user-started `claude` (never spawned/discovered by herd) gets a placement row
-- like every other tracked session. Without it that session has no (socket,
-- window_id) -> pk mapping, so reconcile can neither fill its pid (W3c) nor avoid
-- inserting a DUPLICATE live row (W3a's NOT EXISTS is vacuously true). This is the
-- ONLY writer of source='hook' — the value the CHECK reserved for exactly this.
--
-- The hook holds BOTH identity keys at once — Claude's UUID (payload) and the
-- window (env) — which no other actor does: hooks route by UUID, reconcile by
-- window, and this is where the two are welded onto one row.
--
-- pk via a SELECT on session_id, NOT last_insert_rowid(): after W2b_insert's
-- ON CONFLICT DO UPDATE, last_insert_rowid() returns the last INSERTed row, not
-- the updated one. Same keyed-off-tier-1 shape as W6d_rearm_sid. Run inside the
-- SAME run_tx as W2b_insert, so this SELECT sees that row uncommitted.
--
-- job_name / created_at are ABSENT by the mutability contract (herd.sql:25): a
-- hook session has no job, and a resumed SPAWNED session must not have its job
-- erased. source is NOT in the SET list, so a 'spawn'/'reconcile' row whose W2
-- missed is not downgraded to 'hook'. herd_var is omitted — the hook cannot know
-- it from env; W3b (or spawn) owns it. The trailing WHERE is the same no-op
-- suppressor as W3b: a re-fired hook in an unchanged window writes zero rows.
-- :name W2b_placement
INSERT INTO herd_sessions(session_pk, kitty_socket, window_id, source, verified_at)
SELECT id, :socket, :win, 'hook', :now FROM sessions WHERE session_id = :session_id
ON CONFLICT(session_pk) DO UPDATE SET
    kitty_socket = excluded.kitty_socket,
    window_id    = excluded.window_id,
    verified_at  = excluded.verified_at
WHERE herd_sessions.kitty_socket IS NOT excluded.kitty_socket
   OR herd_sessions.window_id    IS NOT excluded.window_id;


-- ── W3. LIVENESS REAPER (herd/daemon.py) ──────────────────────────────────
-- What "reconcile" shrank to. Hooks own identity, placement, and pid now, so the
-- old `kitten @ ls` discovery (W3a), per-tick placement refresh (W3b) and pid-fill
-- (W3c) are gone. What remains is silent-death detection: a session killed with
-- -9 / crashed / whose terminal closed fires no SessionEnd, so its row would sit
-- live forever. The daemon polls the PROCESS TABLE and reaps.
--
-- LIVENESS COMES FROM THE PROCESS TABLE, NEVER FROM kitty. Absence from a kitty
-- `ls` is evidence about PLACEMENT: a socket blip, an `ls` timeout,
-- allow_remote_control off, or a missed socket would each mass-reap every live row
-- at once. When a claude really dies, `ps` says so within a tick. daemon._dead()
-- also treats state 'Z' as dead (a zombie still passes kill -0) and a recycled pid
-- (comm != claude) as dead. See src/herd/daemon.py.

-- W3d: reap one session whose pid the daemon found dead. Setting stopped_at makes
-- every liveness JOIN see it dead -> its window and job free automatically (no
-- trigger). status_source='pid' records that death was inferred, not hook-reported.
-- :name W3d_reap
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE id = :pk AND stopped_at IS NULL;

-- W3e: BOOT SWEEP. Run once at startup. After a reboot, rows are still
-- stopped_at IS NULL and their pids may have been recycled by unrelated
-- processes — so W3d's liveness check can read a dead session as alive AND
-- idx_sessions_pid_live can silently reject the new session's INSERT. Closes
-- that without a pid_start_time column.
-- :name W3e_boot_sweep
UPDATE sessions SET status = 'stopped', status_source = 'pid',
                    stopped_at = :now, updated_at = :now
WHERE stopped_at IS NULL AND started_at < :boot_time;


-- ── W4. LIFECYCLE HOOKS (core) ────────────────────────────────────────────
-- Every lifecycle hook writes last_event_* in the SAME statement as its event
-- insert. One extra column write on a fork we already pay for.
--
-- THERE IS NO `AND status IS NOT :status` GUARD HERE, AND THERE MUST NEVER BE.
-- It looks like a free no-op suppressor and is in fact the whole thesis
-- failing: post_tool_use.sh is the hot path and always passes status='working',
-- so once status is already 'working' the guard suppresses the ENTIRE update —
-- including last_event_at. Measured: 5 consecutive tool calls matched 0 rows
-- and last_event_at never moved. A session actively running tools then reads as
-- silent, herd's silence rule trips, and it PAGES YOU about a busy session.
-- W4_event_log is unguarded, so events/ would record all 5 while sessions
-- disagreed — and the TUI reads only sessions.
-- klawde's idle column reads ~0s forever; this reads infinity. Same bug, mirrored.
-- Any suppressor must gate on nothing that carries a clock.
-- :name W4_event
UPDATE sessions
SET status          = :status,
    status_source   = 'hook',
    last_event_at   = :now,
    last_event_type = :etype,
    updated_at      = :now
WHERE session_id = :session_id;
-- :name W4_event_log
INSERT INTO events(session_pk, event_type, source, timestamp, raw_json)
SELECT id, :etype, 'hook', :now, :raw FROM sessions WHERE session_id = :session_id;
-- post_tool_use.sh passes :raw = NULL — it fires per tool call.

-- W4b: SESSION END. The only hook-driven death, and distinct from W3d_reap:
-- this one KNOWS (status_source='hook'), where reconcile only INFERS from the
-- process table ('pid'). Setting stopped_at makes every liveness JOIN see the
-- session as dead — job name and window slot free automatically, no trigger.
--
-- session_end.sh MUST be registered BLOCKING, never async. An async hook can be
-- killed when the session exits, leaving stopped_at NULL and the session
-- appearing live until reconcile notices. On `/clear` Claude emits SessionEnd
-- then SessionStart for the NEW session in the SAME window: if the end hasn't
-- landed first, two sessions read as live in one window — harmless now (the
-- JOIN + reconcile's rebuild resolve it), but the death should still land
-- promptly. klawde's own live settings.json still has the async bug its commit
-- message warns against.
-- :name W4_end
UPDATE sessions
SET status          = 'stopped',
    status_source   = 'hook',
    stopped_at      = :now,
    last_event_at   = :now,
    last_event_type = 'end',
    updated_at      = :now
WHERE session_id = :session_id AND stopped_at IS NULL;


-- ── W5. STATUSLINE (core) — UPDATE ONLY ───────────────────────────────────
-- Fires ~1/sec per session. Guarded upstream by the fingerprint diff-cache
-- (tmpfs file per session) so an unchanged payload costs ZERO sqlite3 forks.
-- MUST NOT: create rows (would resurrect stopped sessions / invent empty cwd),
--           touch last_event_* (that is the whole idle-signal thesis).
-- :name W5_statusline
UPDATE sessions SET
    model                = COALESCE(:model, model),
    session_name         = COALESCE(:sname, session_name),
    context_percent      = COALESCE(CAST(:ctx AS INTEGER), context_percent),
    total_cost_usd       = COALESCE(:cost, total_cost_usd),
    git_branch           = COALESCE(:branch, git_branch),
    -- rate limits: payload gives resets_at as a UNIX EPOCH, converted to ISO in
    -- SQLite (zero date forks). NULL-safe -- bind() emits NULL for an absent
    -- value, strftime(...,NULL,...) yields NULL, COALESCE keeps the prior value.
    -- NB inline comments must not contain a statement terminator char, since
    -- both statement parsers cut at the first one they see.
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
    updated_at           = :now       -- NOT last_event_at.
WHERE session_id = :session_id AND stopped_at IS NULL;
-- The prev_cost pair is correct as written: in SQLite an UPDATE's RHS sees the
-- OLD row, so prev_cost_usd captures the previous total before this statement
-- overwrites it. Don't "fix" it.

-- W5b: statusline ADOPTION (Path C). The statusline script is a child of
-- claude, so it inherits $KITTY_WINDOW_ID / $KITTY_LISTEN_ON exactly as a hook
-- does — it never needed them in the payload. That lets a reconciled session
-- pick up metrics with no hooks wired.
-- Still an UPDATE of an existing row: statusline never creates one.
-- Same liveness JOIN as W2: bind the LIVE row in this window, never a dead
-- predecessor that still owns the same (socket, window_id).
-- :name W5b_adopt
UPDATE sessions
SET session_id = :session_id, updated_at = :now
WHERE id = (SELECT h.session_pk FROM herd_sessions h
            JOIN sessions s ON s.id = h.session_pk
            WHERE h.kitty_socket = :socket AND h.window_id = :win
              AND s.stopped_at IS NULL)
  AND session_id IS NULL
  AND stopped_at IS NULL;


-- ── W6. PAGER (herd/pager, TUI tick) ──────────────────────────────────────
-- W6a: arm — the silence rule tripped for the first time.
-- :name W6a_arm
INSERT INTO herd_attention(session_pk, attention_at, paged_level)
VALUES(:pk, :now, 0)
ON CONFLICT(session_pk) DO UPDATE SET attention_at = COALESCE(attention_at, :now);

-- W6b: page fired — record the action + rung.
-- :name W6b_paged
UPDATE herd_attention SET paged_at = :now, paged_level = :level WHERE session_pk = :pk;

-- W6c: ack. Implicit (focus) or explicit (dismiss).
-- The attention_at guard closes a real race: a hook raising a NEW attention
-- while you are mid-jump for a previous one would otherwise be acked unseen.
-- :name W6c_ack
UPDATE herd_attention SET ack_at = :now
WHERE session_pk = :pk
  AND ack_at IS NULL
  AND attention_at IS NOT NULL
  AND attention_at <= :focus_started_at;

-- W6d: RE-ARM. A new semantic event clears the whole row, so the rule may trip
-- fresh. This is what makes ack mean "I've seen THIS silence" rather than
-- "never bother me about this session again".
-- :name W6d_rearm
DELETE FROM herd_attention WHERE session_pk = :pk;

-- W6d_sid: same re-arm, keyed by Claude's UUID instead of the surrogate pk.
-- The pager (python) works from R1 and holds :pk; a HOOK only has session_id,
-- so stop.sh needs this variant. Without it the hook would inline its own
-- DELETE — a write path outside this file, invisible to the check-47 drift
-- guard, re-introducing the hand-quoting bind() exists to kill. Same
-- keyed-two-ways pattern as W2_adopt (socket,window) vs W2b_insert (session_id).
-- :name W6d_rearm_sid
DELETE FROM herd_attention
WHERE session_pk = (SELECT id FROM sessions WHERE session_id = :session_id);


-- ── R_statusline. RENDER INPUTS (core/statusline.sh) ──────────────────────
-- The herd status line shows the job name (tier 2) and a burn rate (from the
-- prev_cost pair W5 maintains) — neither is in the payload. One read per
-- fingerprint MISS feeds the render; unchanged ticks print the cached line and
-- never run this. LEFT JOIN so an unadopted/untracked session still returns a
-- row (empty job). `|`-joined for a single-field bash read.
-- :name R_statusline
SELECT COALESCE(h.job_name,'') || '|' || COALESCE(s.prev_cost_usd,'')
       || '|' || COALESCE(s.prev_cost_sampled_at,'')
FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk = s.id
WHERE s.session_id = :session_id;


-- ── R_job_live. RECYCLABLE-HANDLE CHECK (herd/kitty spawn) ─────────────────
-- Job names are recyclable handles: `herd new api` must work again tomorrow
-- after today's `api` died. There is no UNIQUE index enforcing this anymore
-- (that needed the `live` denormalization, which was the resume-desync bug).
-- Instead spawn asks: does a LIVE session already hold this name? Liveness is
-- the session's own stopped_at, by JOIN — recyclability falls out for free,
-- history is retained (dead rows keep their job_name). Returns a row iff taken.
-- :name R_job_live
SELECT h.session_pk FROM herd_sessions h
JOIN sessions s ON s.id = h.session_pk
WHERE h.job_name = :job AND s.stopped_at IS NULL;


-- ── R1. THE TUI's MAIN READ ───────────────────────────────────────────────
-- One query, all four tables. Attention-first ordering.
-- :name R1_list
SELECT s.id, s.session_id, s.pid, s.cwd, s.status, s.model, s.session_name,
       s.context_percent, s.total_cost_usd, s.git_branch,
       s.last_event_at, s.last_event_type, s.started_at, s.updated_at,
       h.job_name, h.kitty_socket, h.window_id,
       h.herd_var, h.source, h.verified_at,
       a.attention_at, a.paged_at, a.paged_level, a.ack_at
FROM sessions s
LEFT JOIN herd_sessions  h ON h.session_pk = s.id
LEFT JOIN herd_attention a ON a.session_pk = s.id
WHERE s.stopped_at IS NULL
ORDER BY a.attention_at IS NULL,   -- attention first
         a.attention_at ASC,       -- longest-waiting first
         s.started_at DESC;
