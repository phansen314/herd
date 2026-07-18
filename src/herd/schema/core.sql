-- herd TIER 1: Claude Code session facts. See DESIGN.md#tiers.
-- INVARIANT: nothing here may mention herd (table/column/index/trigger).
-- Enforced by the test suite check A. Direction: tier2->tier1 writes ok,
-- tier1->tier2 forbidden.
--
-- WRITERS: session_start.sh (adopt/insert), session_end.sh (stopped_at),
-- stop.sh (last_event_*, 'waiting'), notification.sh ('needs_approval'),
-- post_tool_use.sh ('working', HOT PATH), statusline.sh (metrics + updated_at
-- ONLY, never last_event_*), daemon (stopped_at on silent death, W3d/W3e).

-- auto_vacuum MUST precede any populate; a journal_mode change counts as one.
PRAGMA auto_vacuum=INCREMENTAL;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    -- identity spine. See DESIGN.md#identity. AUTOINCREMENT (not bare rowid) so
    -- the surrogate is never reused once a real DELETE/prune lands.
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT UNIQUE,   -- Claude's UUID. NULL until adopted (UNIQUE ignores NULLs).
    pid                 INTEGER,       -- Claude's own pid (ppid-walk). Liveness oracle. See DESIGN.md#pid.

    -- location
    cwd                 TEXT NOT NULL,
    original_cwd        TEXT,          -- worktree.original_cwd — ONLY set during a --worktree session
    git_branch          TEXT,          -- derived: pure-bash walk to .git/HEAD
    git_worktree        TEXT,          -- workspace.git_worktree — the linked-worktree NAME.
                                       -- Absent (NULL) in a main working tree, which is the
                                       -- common case; unlike worktree.*, it is set for ANY
                                       -- `git worktree add` checkout, not just --worktree runs.

    -- claude's identity for this session. session_name is Claude's /rename name,
    -- user-mutable; deliberately NOT herd's job_name (aliasing would break the handle).
    session_name        TEXT,
    model               TEXT,
    transcript_path     TEXT,
    claude_code_version TEXT,
    output_style        TEXT,

    -- claude's state (the VALUE is always Claude's). status_source is observation
    -- provenance only: hook (reported) | reconcile (discovery inferred) | pid (liveness inferred).
    status              TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (status IN ('working','waiting','needs_approval',
                                          'stopped','unknown')),
    status_source       TEXT CHECK (status_source IN ('hook','reconcile','pid')),

    -- the two clocks. DO NOT CONFLATE. See DESIGN.md#two-clocks.
    -- last_event_at: semantic activity, lifecycle hooks ONLY. updated_at: any write.
    -- The gap is the attention signal. statusline MUST NOT touch last_event_*.
    last_event_at       TEXT,
    last_event_type     TEXT,          -- start|tool|stop|notify|end

    -- metrics (statusline-owned; UPDATE only, never INSERT).
    context_percent      INTEGER,
    context_window_size  INTEGER,      -- max tokens for the model (200k, or 1M extended)
    exceeds_200k_tokens  INTEGER,      -- 0/1; coerced from a bool in the hook's jq
    -- NOT cumulative session totals despite the names. Since Claude Code v2.1.132
    -- these are what is currently IN the context window, from the latest API
    -- response — a gauge, not a counter. Named to match klawde's schema.
    total_input_tokens   INTEGER,
    total_output_tokens  INTEGER,
    total_cost_usd       REAL,
    prev_cost_usd        REAL,   -- burn-rate delta pair; resampled when >300s old
    prev_cost_sampled_at TEXT,
    api_duration_ms      INTEGER,
    lines_added          INTEGER,
    lines_removed        INTEGER,

    -- account-level (denormalized per session; latest row wins)
    rate_limit_5h_percent   REAL,
    rate_limit_5h_resets_at TEXT,
    rate_limit_7d_percent   REAL,
    rate_limit_7d_resets_at TEXT,

    -- lifecycle
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,   -- ANY write. Not an idle signal.
    stopped_at          TEXT             -- NULL == live. Drives partial idx.
);

-- At most one LIVE session per pid — makes the identity merge safe by
-- construction. Dead rows fall out, so pid reuse after a clean end is fine.
-- Reboot pid-reuse caveat closed by the boot sweep (W3e). See DESIGN.md#pid.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_pid_live
    ON sessions(pid) WHERE stopped_at IS NULL AND pid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_live
    ON sessions(stopped_at, status);

CREATE INDEX IF NOT EXISTS idx_sessions_unadopted
    ON sessions(pid) WHERE session_id IS NULL AND stopped_at IS NULL;
