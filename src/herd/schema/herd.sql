-- herd TIER 2: herd's relationship to a session. See DESIGN.md#tiers.
-- All FKs point at sessions(id) — the surrogate — never session_id (NULL until
-- adopted). Applied AFTER core.sql against the same DB file.

-- ── HERD_SESSIONS — merged placement + job, one row per known session. Liveness is
-- NOT stored here: it lives in sessions.stopped_at, read by JOIN — a `live` column
-- here once desynced permanently on resume. See DESIGN.md#liveness.
-- Mutability contract (test_mutability.py::test_refire_mutability_contract):
--   IMMUTABLE: job_name, created_at (set at spawn); herd_var (a hook can't know the
--              spawn var); source (provenance must not decay to 'hook')
--   MUTABLE  (a hook re-fire may overwrite): kitty_socket, window_id, verified_at,
--            tab_title
CREATE TABLE IF NOT EXISTS herd_sessions (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    -- herd's job identity. NULL for sessions herd didn't spawn. NOT
    -- sessions.session_name (user-mutable, would break the handle).
    job_name     TEXT,
    created_at   TEXT,

    -- window_id is MEANINGLESS without the socket — listen_on unix:/tmp/kitty-{pid}
    -- gives each kitty its own id space.
    kitty_socket TEXT NOT NULL,   -- $KITTY_LISTEN_ON
    window_id    INTEGER,         -- $KITTY_WINDOW_ID. (socket, window_id) is the whole jump key.
    herd_var     TEXT,            -- HERD_JOB user var (--var). Identifies a WINDOW, not a
                                  -- session (sticky, survives claude exit) — AND with pid
                                  -- liveness. Match values are unanchored regex, so anchor them.
    source       TEXT NOT NULL CHECK (source IN ('spawn','hook')),
                              -- 'spawn' (W1) or 'hook' (W2b_placement).
    verified_at  TEXT NOT NULL, -- re-stamped when focus re-derives placement.

    -- The kitty TAB title, captured live by tab_sync.sh (UserPromptSubmit) via
    -- W7_tab_title. The ONE piece of kitty render state herd persists: a dead
    -- session can't be re-derived from `kitten @ ls`, and restart needs its real
    -- title. See DESIGN.md#restart. NULL until first captured / outside kitty.
    tab_title    TEXT
);

-- PLAIN, non-unique lookups. Uniqueness ("one live session per window", "recyclable
-- job names") is a property of the JOIN to sessions.stopped_at, NOT of the schema —
-- enforcing it in the DB needed the `live` denorm that desynced.
CREATE INDEX IF NOT EXISTS idx_herd_window
    ON herd_sessions(kitty_socket, window_id);
CREATE INDEX IF NOT EXISTS idx_herd_job
    ON herd_sessions(job_name);

-- NO TRIGGER. Tier 1 has ZERO tier-2 machinery attached — the boundary is
-- strictly one-way (tier2->tier1 via FK only).


-- ── HERD_ATTENTION — records what herd DID, not what it thinks. "Needs attention"
-- is DERIVED every tick, never stored; only the EDGE and the ACK persist (else a 1s
-- poll re-decides 60x/min). See DESIGN.md#attention.
-- Kept out of herd_sessions: it is the only row written every tick, and
-- DELETE-the-row is a meaningful "re-evaluate everything".
-- WRITER: the daemon's tick — NOT hooks, which cannot detect silence.
CREATE TABLE IF NOT EXISTS herd_attention (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    attention_at TEXT,    -- the EDGE: when the silence rule first tripped (not last_event_at's age).
    ack_at       TEXT     -- written by a jump, the only ack path. Means "seen THIS silence":
                          -- the CLI hides the mark while it is set and the row STAYS armed. Also the
                          -- timer restart — the daemon re-notifies once the status threshold of
                          -- silence has passed since the ack.
                          -- A jump must NOT delete the row instead: W6d is a whole-row DELETE, so it
                          -- takes ack_at with it and the next tick re-arms off the still-old
                          -- last_event_at, flapping forever.
);

CREATE INDEX IF NOT EXISTS idx_herd_attention_active
    ON herd_attention(attention_at) WHERE attention_at IS NOT NULL;
