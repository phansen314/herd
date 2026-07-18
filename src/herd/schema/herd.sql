-- herd TIER 2: herd's relationship to a session (where is it, what did I name
-- it, have I bothered you about it). See DESIGN.md#tiers.
-- All FKs point at sessions(id) — the surrogate — never session_id (NULL until
-- adopted). Applied AFTER core.sql against the same DB file.

-- ── HERD_SESSIONS — merged placement + job, one row per known session.
-- Liveness is NOT stored here: it lives in sessions.stopped_at, read by JOIN.
-- A `live` column here once desynced permanently on resume. See DESIGN.md#liveness.
-- Mutability contract (enforced by discipline, the test suite section D):
--   IMMUTABLE (set at spawn): job_name, created_at
--   MUTABLE  (hook re-fire may overwrite): kitty_socket, window_id, herd_var, source, verified_at
-- Placement is a CACHE re-derived on the focus path, not a fact. See DESIGN.md#focus--jump-kittyfocuspy-clipy.
CREATE TABLE IF NOT EXISTS herd_sessions (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    -- immutable: herd's job identity. NULL for sessions herd didn't spawn.
    -- NOT sessions.session_name (that one is user-mutable, would break the handle).
    job_name     TEXT,
    created_at   TEXT,

    -- mutable: kitty placement. window_id is MEANINGLESS without the socket —
    -- listen_on unix:/tmp/kitty-{pid} gives each kitty its own id space.
    kitty_socket TEXT NOT NULL,   -- $KITTY_LISTEN_ON
    window_id    INTEGER,         -- $KITTY_WINDOW_ID. (socket, window_id) is the whole jump key.
    herd_var     TEXT,            -- HERD_JOB user var (--var). Identifies a WINDOW, not a
                                  -- session (sticky, survives claude exit) — AND with pid
                                  -- liveness. Match values are unanchored regex; anchor them.
    source       TEXT NOT NULL CHECK (source IN ('spawn','hook')),
                              -- 'spawn' (W1) or 'hook' (W2b_placement).
    verified_at  TEXT NOT NULL    -- staleness made explicit; TUI renders old placement as degraded.
);

-- PLAIN, non-unique lookups. Uniqueness ("one live session per window",
-- "recyclable job names") is a property of the JOIN to sessions.stopped_at, NOT
-- of the schema — a DB-enforced version needed the `live` denorm that desynced.
-- See DESIGN.md#liveness.
CREATE INDEX IF NOT EXISTS idx_herd_window
    ON herd_sessions(kitty_socket, window_id);
CREATE INDEX IF NOT EXISTS idx_herd_job
    ON herd_sessions(job_name);

-- NO TRIGGER. Tier 1 has ZERO tier-2 machinery attached — the boundary is
-- strictly one-way (tier2->tier1 via FK only).


-- ── HERD_ATTENTION — records what herd DID, not what it thinks. "Needs
-- attention" is DERIVED every tick, never stored; only the ACTION persists (else
-- a 1s poll pages 60x/min). See DESIGN.md#attention.
-- Kept out of herd_sessions: it's the only row written every tick (isolate the
-- write path), and DELETE-the-row is a meaningful "re-evaluate everything".
-- WRITER: the daemon's tick — NOT hooks (hooks can't detect silence). This makes
-- herd a WRITER holding one RW connection; WAL keeps it out of the hooks' way.
CREATE TABLE IF NOT EXISTS herd_attention (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    attention_at TEXT,    -- the EDGE: when the silence rule first tripped (not last_event_at's age).
    paged_at     TEXT,    -- when we last actually notified you
    paged_level  INTEGER NOT NULL DEFAULT 0,   -- escalation rung
    ack_at       TEXT     -- implicit (focus) or explicit (dismiss). Ack means "seen THIS silence";
                          -- the next semantic event clears the row (W6d) so the rule may trip fresh.
);

CREATE INDEX IF NOT EXISTS idx_herd_attention_active
    ON herd_attention(attention_at) WHERE attention_at IS NOT NULL;
