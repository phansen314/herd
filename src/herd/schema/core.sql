-- ═══════════════════════════════════════════════════════════════════════════
-- herd/core/schema.sql — TIER 1: Claude Code session facts
--
-- INVARIANT: nothing in this file may mention herd. No table, column, index,
-- or trigger here may reference placement, job, or attention. If you find
-- yourself wanting to, the concept belongs in herd/schema.sql.
--
-- Direction of allowed dependency:
--     tier2 -> tier1   WRITES ok   (reconcile inserts sessions; herd's trigger
--                                   attaches to sessions but is DECLARED in
--                                   herd/schema.sql)
--     tier1 -> tier2   FORBIDDEN   (enforced in CI via import-linter for the
--                                   Python packages, and by review for SQL)
--
-- WRITERS
--   session_start.sh   INSERT/adopt, status, last_event_*
--   session_end.sh     stopped_at, status
--   stop.sh            last_event_* (turn ended -> the 'waiting' signal)
--   notification.sh    status='needs_approval', last_event_*
--   post_tool_use.sh   status='working', last_event_*   [HOT PATH]
--   statusline.sh      metrics + updated_at ONLY. NEVER last_event_*.
--   herd reconcile     INSERT for undiscovered sessions; pid; stopped_at
-- ═══════════════════════════════════════════════════════════════════════════

-- auto_vacuum MUST precede any populate; a journal_mode change counts as one.
PRAGMA auto_vacuum=INCREMENTAL;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    -- ── identity ─────────────────────────────────────────────────────────
    -- `id` is the spine of the whole design. Tier 2 references THIS, never
    -- session_id. That is what lets a session exist (spawned by herd, or
    -- discovered by reconcile) with a job name, a placement, and pager state
    -- BEFORE Claude Code has told us its UUID. Adoption is then a plain
    -- UPDATE of session_id — no PK rewrite, no cascade.
    -- AUTOINCREMENT, not a bare rowid alias: a bare rowid RECYCLES the highest
    -- id on the next insert after a delete. Sessions are soft-deleted (stopped_at)
    -- today, so it never bites — but the moment a prune job adds a real DELETE, a
    -- recycled id could reattach to a stale :pk held mid-tick by the TUI/pager.
    -- AUTOINCREMENT (sqlite_sequence-backed, like events.id below) makes the
    -- surrogate strictly monotonic and never-reused. Cost is one sqlite_sequence
    -- write per insert — immaterial at the session insert rate.
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT UNIQUE,   -- Claude's UUID. NULL until adopted.
                                       -- SQLite UNIQUE ignores NULLs, so many
                                       -- unadopted rows coexist.
    pid                 INTEGER,       -- liveness oracle (kill -0). Sourced from
                                       -- the SessionStart hook's claude_pid()
                                       -- ppid-walk (common.sh). SPIKE-1 (pid must
                                       -- come from `kitten @ ls`) is OVERTURNED:
                                       -- the blocking hook is a live descendant of
                                       -- claude, so the walk is exact and cleaner
                                       -- than the ls route (MCP children and other
                                       -- sessions are siblings, never ancestors).
                                       --
                                       -- LOAD-BEARING: this is CLAUDE's pid (walked
                                       -- to the first ancestor comm=='claude'),
                                       -- NEVER the window shell — that outlives
                                       -- claude and would make the session
                                       -- immortal. W2c_pid_claim keeps one LIVE row
                                       -- per pid (idx_sessions_pid_live) by reaping
                                       -- a stale holder before a new claim.

    -- ── location ─────────────────────────────────────────────────────────
    cwd                 TEXT NOT NULL,
    original_cwd        TEXT,          -- worktree.original_cwd
    git_branch          TEXT,          -- derived: pure-bash walk to .git/HEAD
    git_worktree        TEXT,

    -- ── claude's identity for this session ───────────────────────────────
    -- session_name is CLAUDE's name and is user-mutable from inside the
    -- session. It is deliberately NOT herd's job name (see herd_sessions):
    -- aliasing them would let a user rename their session and silently break
    -- herd's handle on it.
    session_name        TEXT,
    model               TEXT,
    transcript_path     TEXT,
    claude_code_version TEXT,
    output_style        TEXT,

    -- ── claude's state — NOT herd's judgment about it ────────────────────
    -- 'waiting' = turn ended, Claude wants input. Requires the Stop hook,
    -- which klawde never wires — which is why klawde has no idle state and
    -- has to filter notification_type='permission_prompt' to keep idle_prompt
    -- from wedging sessions in a false needs_approval.
    status              TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (status IN ('working','waiting','needs_approval',
                                          'stopped','unknown')),
    -- status_source is OBSERVATION PROVENANCE, not herd's relationship to the
    -- session. The status VALUE is always Claude's; this records the channel it
    -- was seen through: 'hook' (Claude reported it), 'reconcile' (herd's
    -- discovery inferred it), 'pid' (a liveness check inferred it). Any tool
    -- observing these sessions would record the same distinction — it lets the
    -- TUI show how much to trust a status. The mechanism names are herd's, but
    -- the fact ("this status was seen via a liveness check") is not.
    status_source       TEXT CHECK (status_source IN ('hook','reconcile','pid')),

    -- ── the two clocks — DO NOT CONFLATE ─────────────────────────────────
    -- last_event_at : semantic activity. Lifecycle hooks ONLY.
    -- updated_at    : any write at all, incl. every statusline tick (~1/sec).
    -- klawde ages updated_at and renders it as an idle column; because
    -- statusline stamps it constantly, that column reads ~0s forever and the
    -- signal is worthless. The GAP between these two clocks is herd's
    -- attention signal. statusline.sh MUST NOT touch last_event_*.
    --
    -- The inverse failure is just as fatal and is easier to write by accident:
    -- if a hook's UPDATE is gated on the status CHANGING, then a busy session
    -- emitting the same status repeatedly (post_tool_use fires per tool call,
    -- always 'working') stops advancing last_event_at and reads as SILENT.
    -- See writes.sql W4 — the guard belongs on nothing that carries a clock.
    last_event_at       TEXT,
    last_event_type     TEXT,          -- start|tool|stop|notify|end

    -- ── metrics (statusline-owned; UPDATE only, never INSERT) ────────────
    -- statusline must never create a row: it would resurrect stopped sessions
    -- and invent rows with empty cwd. Rows are owned by session_start.sh and
    -- reconcile.
    context_percent      INTEGER,
    context_window_size  INTEGER,
    exceeds_200k_tokens  INTEGER,
    total_input_tokens   INTEGER,
    total_output_tokens  INTEGER,
    total_cost_usd       REAL,
    prev_cost_usd        REAL,   -- burn-rate delta pair; resampled when
    prev_cost_sampled_at TEXT,   -- older than 300s
    api_duration_ms      INTEGER,
    lines_added          INTEGER,
    lines_removed        INTEGER,

    -- ── account-level (denormalized per session; latest row wins) ─────────
    rate_limit_5h_percent   REAL,
    rate_limit_5h_resets_at TEXT,
    rate_limit_7d_percent   REAL,
    rate_limit_7d_resets_at TEXT,

    -- ── lifecycle ────────────────────────────────────────────────────────
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,   -- ANY write. Not an idle signal.
    stopped_at          TEXT             -- NULL == live. Drives partial idx.
);

-- At most one LIVE session per pid. Makes reconcile's identity merge safe by
-- construction rather than by careful hook writing. Dead rows fall out of the
-- index, so pid reuse after a session ends is fine.
--
-- CAVEAT (accepted): this holds only while pids are not recycled under a live
-- row. After a reboot, rows left with stopped_at IS NULL can collide with a
-- recycled pid and silently reject the new session's INSERT. Mitigated without
-- a schema change by the boot sweep — reap live rows whose started_at precedes
-- system boot. A pid_start_time column would close it properly; deliberately
-- deferred.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_pid_live
    ON sessions(pid) WHERE stopped_at IS NULL AND pid IS NOT NULL;

-- The TUI's main query filters live rows.
CREATE INDEX IF NOT EXISTS idx_sessions_live
    ON sessions(stopped_at, status);

-- Adoption lookup: reconciled rows still awaiting a session_id.
CREATE INDEX IF NOT EXISTS idx_sessions_unadopted
    ON sessions(pid) WHERE session_id IS NULL AND stopped_at IS NULL;


-- ── EVENTS ─────────────────────────────────────────────────────────────────
-- Append-only forensic trail. The TUI does NOT read this table — the hot-path
-- signal is sessions.last_event_at, maintained by the same hook write. Keeping
-- a reader out of here is deliberate: MAX(timestamp) per session per tick
-- across N sessions against an unbounded table is a cost we can simply not pay.
--
-- Because nothing reads it, this table cannot corroborate last_event_at: if a
-- hook logs an event here but its sessions UPDATE is suppressed, the two
-- disagree in silence and only the wrong one is ever read. Any guard on the
-- lifecycle UPDATE must therefore apply identically to the INSERT, or to
-- neither.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_pk  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    raw_json    TEXT   -- lifecycle only. NEVER from post_tool_use: it fires
                       -- per tool call.
);

CREATE INDEX IF NOT EXISTS idx_events_session
    ON events(session_pk, timestamp);
