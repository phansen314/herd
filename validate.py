"""Validation suite for the herd data model foundation.
Every claim the design rests on is asserted here, not narrated.

Run: python3 validate.py   (no install needed; src/ is put on the path below)
This suite is the ONLY CI gate: import-linter cannot see the tier boundary,
because that boundary is SQL and the hooks are bash. So it lives here."""
import sqlite3, os, pathlib, re, sys, contextlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
from herd.db import (CORE_SCHEMA, HERD_SCHEMA, WRITES as WRITES_PATH,
                     load_statements, apply_schema)

FAILED = []
def check(name, cond, detail=""):
    if cond: print(f"  \033[32mPASS\033[0m {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  \033[31mFAIL\033[0m {name} — {detail}"); FAILED.append(name)

@contextlib.contextmanager
def guard(name):
    """Turn an unexpected exception into a FAIL instead of a dead script.

    Without this the suite ABORTS on the first surprising error and every later
    check silently never runs — so it reports fewer failures than exist, which
    is the one thing a validation suite must never do. Found the hard way:
    breaking W2's live=1 guard made a later check raise IntegrityError, killing
    the run at that line and hiding the rest of the section behind a traceback."""
    try:
        yield
    except Exception as e:
        check(name, False, f"raised {type(e).__name__}: {e}")

CORE = CORE_SCHEMA.read_text()
HERD = HERD_SCHEMA.read_text()
WRITES = WRITES_PATH.read_text()
T0 = "2026-07-15T10:00:00.000Z"
T1 = "2026-07-15T10:05:00.000Z"
T2 = "2026-07-15T10:10:00.000Z"

# The loader is production code (herd.db), not a copy living in the suite —
# reconcile will load the same statements the same way, so using it here tests it.
W = load_statements()

def fresh(tier2=True):
    if os.path.exists("f.db"): os.remove("f.db")
    for s in ("f.db-wal","f.db-shm"):
        if os.path.exists(s): os.remove(s)
    c = sqlite3.connect("f.db")
    # autocommit. Section N runs the real bash hooks against this file from
    # ANOTHER connection, and python's default isolation_level would leave the
    # setup rows in an uncommitted transaction that close() silently rolls back
    # — the hook then sees an empty database. That produced a *false pass*:
    # check 48 lost its reconciled row, W2b inserted a fresh one that landed on
    # the same rowid it was asserting against, and the adopt path was never
    # exercised at all.
    c.isolation_level = None
    apply_schema(c, tier2=tier2)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c

print("\n\033[1m═══ A. TIER BOUNDARY (source-level) ═══\033[0m")
core_l = CORE.lower()
# strip comments before scanning for herd references
core_code = "\n".join(l.split("--")[0] for l in CORE.splitlines()).lower()
check("schema/core.sql contains no herd_ tables",
      "herd_" not in core_code, "scanned non-comment DDL")
check("schema/core.sql declares no triggers",
      "create trigger" not in core_code)
herd_code = "\n".join(l.split("--")[0] for l in HERD.splitlines()).lower()
# The decouple removed the ONLY thing tier 2 attached to tier 1. Assert it stays
# gone: no trigger anywhere, and nothing in herd.sql declares ON sessions. The
# boundary is now strictly one-way (tier2 -> tier1 via FK), with zero tier-2
# machinery reaching back into the core table.
check("schema/herd.sql declares no trigger (decoupled)",
      "create trigger" not in herd_code)
check("no tier-2 DDL attaches to the sessions table",
      "on sessions" not in herd_code)
check("no `live` denormalization column anywhere",
      "live" not in "".join(l.split("--")[0] for l in HERD.splitlines()).lower())

# ── 45/46. THE TIER THESIS, EXECUTED ──────────────────────────────────────
# Tier 1 claims to be "facts that would be true if herd didn't exist". Until
# now that was asserted by grepping for the string 'herd_' — but the schema was
# never once applied WITHOUT tier 2. These two run it.
with guard("45 tier 1 applies standalone (herd absent entirely)"):
    c = fresh(tier2=False)
    t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO events(session_pk,event_type,source,timestamp) VALUES(?,'start','hook',?)",(pk,T0))
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T1,pk))   # no trigger to fire
    check("45 tier 1 applies standalone (herd absent entirely)",
          t == ['events','sessions'],
          f"a herd-less install is a working install; tables={t}")

# 46 changed with the decouple. It used to prove the dependency at DDL time —
# CREATE TRIGGER ... ON sessions failed if sessions didn't exist. With the
# trigger gone, herd.sql applies standalone (SQLite does not validate FK parent
# tables at CREATE TABLE time). So the dependency is now purely a RUNTIME fact:
# tier 2 is inert without tier 1. Assert BOTH halves — the DDL change AND that
# tier 2 remains non-functional alone.
with guard("46 tier 2 applies standalone but is inert without tier 1"):
    if os.path.exists("g.db"): os.remove("g.db")
    c2 = sqlite3.connect("g.db"); c2.isolation_level = None
    c2.executescript(HERD)                       # applies standalone now — the new fact
    c2.execute("PRAGMA foreign_keys=ON")
    inert = False
    try:
        # every use of herd_sessions must reach `sessions`; with it absent, even
        # an insert cannot resolve its FK parent — tier 2 does nothing on its own.
        c2.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,source,verified_at)"
                   " VALUES(1,'k','spawn',?)",(T0,))
    except sqlite3.OperationalError:
        inert = True
    check("46 tier 2 applies standalone but is inert without tier 1", inert,
          "herd.sql creates its tables alone, but every row it holds dangles off "
          "sessions — the direction is real, just no longer DDL-enforced")
    c2.close(); os.remove("g.db")

print("\n\033[1m═══ B. SCHEMA APPLIES ═══\033[0m")
c = fresh()
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
check("four tables", tabs == ['events','herd_attention','herd_sessions','sessions'], str(tabs))
check("WAL mode", c.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
check("auto_vacuum=INCREMENTAL", c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2)
check("idempotent re-apply", (c.executescript(CORE), c.executescript(HERD), True)[-1])

print("\n\033[1m═══ C. THE SURROGATE KEY (the spine) ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at) "
               "VALUES('/code/app','unknown','reconcile',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "os_window_id,tab_id,window_id,herd_var,source,verified_at) "
          "VALUES(?,?,?,?,?,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",1,3,7,"api-refactor",T0))
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,T0))
check("tier-2 rows exist with session_id NULL",
      c.execute("SELECT session_id FROM sessions WHERE id=?",(pk,)).fetchone()[0] is None,
      "spawned session has job+placement+attention before Claude reports a UUID")
# adopt — the SHIPPING W2
n = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                              "transcript":"/t.jsonl","now":T1,
                              "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt via (socket,window_id)", n == 1)
r = c.execute("""SELECT s.session_id,h.job_name,h.window_id,a.attention_at
                 FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk=s.id
                 LEFT JOIN herd_attention a ON a.session_pk=s.id WHERE s.id=?""",(pk,)).fetchone()
check("tier-2 survives adoption", tuple(r) == ("a3f9-uuid","api-refactor",7,T0), str(tuple(r)))
n2 = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                               "transcript":"/t.jsonl","now":T1,
                               "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt is idempotent", n2 == 0, "re-fire is a no-op")
check("multiple unadopted rows coexist (UNIQUE ignores NULL)",
      (c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/x',?,?)",(T0,T0)),
       c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/y',?,?)",(T0,T0)),
       c.execute("SELECT COUNT(*) FROM sessions WHERE session_id IS NULL").fetchone()[0])[-1] == 2)

print("\n\033[1m═══ D. MUTABILITY CONTRACT (reconcile must not clobber) ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,herd_var,source,verified_at) VALUES(?,?,?,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",7,"api-refactor",T0))
c.execute(W["W3b_placement"], {"pk":pk,"socket":"unix:/tmp/kitty-9","oswin":2,"tab":5,
                               "win":42,"title":"new title","var":None,"now":T2})
r = c.execute("SELECT job_name,created_at,window_id,tab_id,source,herd_var,verified_at "
              "FROM herd_sessions WHERE session_pk=?",(pk,)).fetchone()
check("reconcile preserves job_name", r["job_name"] == "api-refactor")
check("reconcile preserves created_at", r["created_at"] == T0)
check("reconcile preserves herd_var (COALESCE)", r["herd_var"] == "api-refactor",
      "NULL from reconcile must not erase the spawn-time var")
check("reconcile preserves source='spawn'", r["source"] == "spawn", "provenance doesn't decay")
check("reconcile DOES update window_id", r["window_id"] == 42)
check("reconcile DOES update tab_id", r["tab_id"] == 5)
check("reconcile DOES update verified_at", r["verified_at"] == T2)

print("\n\033[1m═══ E. JOB NAMES: recyclable handles via R_job_live (no trigger, no UNIQUE) ═══\033[0m")
# Recyclability is now the R_job_live check + sessions.stopped_at, not a partial
# UNIQUE index maintained by a trigger. "Taken" == a LIVE session holds the name.
c = fresh()
def job_holder(conn, job):
    r = conn.execute(W["R_job_live"], {"job": job}).fetchone()
    return r["session_pk"] if r else None

p1 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p1,"api-refactor",T0,"unix:/tmp/k1",7,T0))
check("R_job_live reports the live holder", job_holder(c, "api-refactor") == p1,
      "spawn checks this and refuses a name a live session already holds")
# the DB does NOT reject a duplicate insert anymore — the app must consult
# R_job_live first. Prove the constraint is gone (so a stale/dead row can't block):
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T2,p1))   # holder dies (just stopped_at)
check("death frees the name with no trigger", job_holder(c, "api-refactor") is None,
      "setting stopped_at makes R_job_live see it dead — no live column to desync")
# reuse: session 2 takes the name. history retained (both rows keep job_name).
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p2,"api-refactor",T2,"unix:/tmp/k1",8,T2))
check("name reusable after death",
      c.execute("SELECT COUNT(*) FROM herd_sessions WHERE job_name='api-refactor'").fetchone()[0] == 2,
      "history retained: 2 rows, one live one dead")
check("R_job_live returns exactly the LIVE holder among history",
      job_holder(c, "api-refactor") == p2,
      "dead + live rows share the name; the JOIN picks the live one")
# NULL job_name (reconciled sessions) never collides and is never 'held'
p3 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/c',?,?)",(T0,T0)).lastrowid
p4 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/d',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'reconcile',?)",(p3,"unix:/tmp/k1",20,T0))
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'reconcile',?)",(p4,"unix:/tmp/k1",21,T0))
check("many NULL job_names coexist", job_holder(c, None) is None, "reconciled sessions have no job")

print("\n\033[1m═══ F. PID / LIVENESS ═══\033[0m")
c = fresh()
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at,stopped_at) VALUES(4821,'/a',?,?,?)",(T0,T0,T1))
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at,stopped_at) VALUES(4821,'/a',?,?,?)",(T0,T0,T1))
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(4821,'/a',?,?)",(T0,T0))
check("many DEAD sessions may share a pid", True, "pid reuse after death is legal")
try:
    c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(4821,'/b',?,?)",(T0,T0))
    check("only one LIVE session per pid", False, "two live rows allowed!")
except sqlite3.IntegrityError:
    check("only one LIVE session per pid", True)
c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/x',?,?)",(T0,T0))
c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/y',?,?)",(T0,T0))
check("many live sessions with NULL pid coexist", True, "pid unknown before reconcile")

print("\n\033[1m═══ G. THE TWO CLOCKS (the idle-signal thesis) ═══\033[0m")
c = fresh()
c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
          " VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
# statusline ticks repeatedly — must move updated_at, must NOT move last_event_at
for t in (T1,T2):
    c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":42,"cost":1.5,
                                   "branch":None,"now":t,"session_id":"s1"})
r = c.execute("SELECT last_event_at,updated_at FROM sessions WHERE session_id='s1'").fetchone()
check("statusline moves updated_at", r["updated_at"] == T2)
check("statusline does NOT move last_event_at", r["last_event_at"] == T0,
      "the gap between clocks IS the attention signal")
gap = "10:00 -> 10:10 = 10min of true silence, visible"
check("idle signal is real", r["last_event_at"] != r["updated_at"], gap)

print("\n\033[1m═══ H. STATUSLINE MUST NOT CREATE OR RESURRECT ═══\033[0m")
c = fresh()
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,
                                   "branch":None,"now":T1,"session_id":"ghost"}).rowcount
check("statusline on unknown session is a no-op", n == 0,
      "UPDATE-only: never invents rows with empty cwd")
c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at,stopped_at)"
          " VALUES('dead','/a','stopped',?,?,?)",(T0,T0,T1))
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,
                                   "branch":None,"now":T2,"session_id":"dead"}).rowcount
check("statusline cannot resurrect a stopped session", n == 0)

print("\n\033[1m═══ I. PAGER LIFECYCLE ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
               " VALUES('s1','/a','waiting',?,'stop',?,?)",(T0,T0,T0)).lastrowid
c.execute(W["W6a_arm"], {"pk":pk,"now":T1})
c.execute(W["W6a_arm"], {"pk":pk,"now":T2})   # tick again — must not move the edge
check("attention_at is an EDGE, not re-stamped",
      c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0] == T1,
      "COALESCE preserves first trip")
c.execute(W["W6b_paged"], {"now":T2,"level":2,"pk":pk})
c.execute(W["W6c_ack"], {"now":T2,"pk":pk,"focus_started_at":T2})
r = c.execute("SELECT attention_at,paged_at,paged_level,ack_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()
check("page + ack recorded", tuple(r) == (T1,T2,2,T2), str(tuple(r)))
c.execute(W["W6d_rearm"], {"pk":pk})
check("re-arm clears row", c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]==0,
      "ack means 'I saw THIS silence', not 'never bother me again'")
c.execute(W["W6a_arm"], {"pk":pk,"now":T2})
check("rule can trip fresh after re-arm",
      c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]==T2)

print("\n\033[1m═══ J. CASCADE ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,?,?,'spawn',?)",(pk,"j",T0,"unix:/tmp/k1",7,T0))
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,T0))
c.execute("INSERT INTO events(session_pk,event_type,source,timestamp) VALUES(?,'start','hook',?)",(pk,T0))
c.execute("DELETE FROM sessions WHERE id=?",(pk,))
orph = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE session_pk=?",(pk,)).fetchone()[0]
        for t in ("herd_sessions","herd_attention","events")}
check("ON DELETE CASCADE cleans all tier-2 + events", sum(orph.values())==0, str(orph))

print("\n\033[1m═══ K. CONSTRAINTS ═══\033[0m")
c = fresh()
for col,val,tab in (("status","bogus","sessions"),("status_source","nope","sessions")):
    try:
        c.execute(f"INSERT INTO sessions(cwd,{col},started_at,updated_at) VALUES('/z',?,?,?)",(val,T0,T0))
        check(f"{col} CHECK rejects garbage", False, "accepted!")
    except sqlite3.IntegrityError:
        check(f"{col} CHECK rejects garbage", True)
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
try:
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'bogus',?)",(pk,"s",1,T0))
    check("herd source CHECK rejects garbage", False, "accepted!")
except sqlite3.IntegrityError:
    check("herd source CHECK rejects garbage", True)
try:
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(999,?,?,'spawn',?)",("s",1,T0))
    check("FK enforced on herd_sessions", False, "orphan accepted!")
except sqlite3.IntegrityError:
    check("FK enforced on herd_sessions", True)
# (socket,window_id) is DELIBERATELY NOT unique anymore. A dead row and a live
# row may share a window — that is what makes window reuse and resume work
# without a `live` denormalization. The liveness JOIN separates them; reconcile's
# ground-truth rebuild keeps at most one LIVE row per window (an app invariant,
# tested in the reconcile fixture, not by a DB constraint).
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(pk,"unix:/tmp/k1",7,T0))
c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T1,pk))   # first holder dies
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(p2,"unix:/tmp/k1",7,T0))
check("(socket,window_id) may repeat across a dead+live pair", True,
      "no UNIQUE constraint — window is a recyclable handle; JOIN tells them apart")
live_in_win = c.execute(
    "SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
    "WHERE h.kitty_socket='unix:/tmp/k1' AND h.window_id=7 AND s.stopped_at IS NULL").fetchall()
check("exactly one LIVE session resolves for the reused window",
      [r["session_pk"] for r in live_in_win] == [p2],
      "the JOIN returns only the live occupant; the dead row is history")

print("\n\033[1m═══ L. TUI MAIN QUERY ═══\033[0m")
c = fresh()
for i,(cwd,st,ev) in enumerate([("/app","waiting","stop"),("/api","working","tool"),("/web","working","tool")]):
    p = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?)",(f"s{i}",cwd,st,T0,ev,T0,T2)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,?,?,'spawn',?)",(p,f"job{i}",T0,"unix:/tmp/k1",i+1,T2))
    if i == 0: c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(p,T1))
Q = W["R1_list"]
plan = [r[-1] for r in c.execute("EXPLAIN QUERY PLAN "+Q)]
for p in plan: print("      ", p)
check("main query uses idx_sessions_live", any("idx_sessions_live" in p for p in plan))
check("tier-2 joins hit PK directly", sum("INTEGER PRIMARY KEY" in p for p in plan) == 2)
rows = c.execute(Q).fetchall()
check("attention-first ordering", rows[0]["cwd"] == "/app" and rows[0]["attention_at"] == T1,
      f"waiting session sorts first; got {[r['cwd'] for r in rows]}")

# ═══════════════════════════════════════════════════════════════════════════
# M. WINDOW REUSE + THE WRITE PATHS
#
# Everything below passed the original 40 checks while being broken. §K only
# ever tested two LIVE sessions in one window; it never asked what happens to
# the ordinary case — a user exits claude and starts another in the same
# window. Green is not the same as complete.
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ M. WINDOW REUSE + WRITE PATHS (41-44) ═══\033[0m")

# ── 41. a window is a RECYCLABLE HANDLE — dead + live rows coexist, JOIN splits ──
SOCK = "unix:/tmp/kitty-20035"
def live_in_window(conn, sock, win):
    return [r["session_pk"] for r in conn.execute(
        "SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
        "WHERE h.kitty_socket=? AND h.window_id=? AND s.stopped_at IS NULL", (sock, win))]

c = fresh()
a = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(111,'/code/herd',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,5,'reconcile',?)",(a,T0,SOCK,T0))
c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
# no UNIQUE index: the reused-window placement INSERT simply succeeds now.
c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,5,'reconcile',?)",(b,T2,SOCK,T2))
check("41 window reuse: new session gets placement in a reused window",
      live_in_window(c, SOCK, 5) == [b],
      "dead A + live B share window 5; the JOIN returns only B (A is history)")
# The 'one live per window' invariant is now app-level (reconcile's rebuild), not
# a DB constraint. The DB DELIBERATELY allows two live rows — assert that, so a
# future reader doesn't expect the old UNIQUE to catch a rebuild bug.
c.execute("UPDATE sessions SET stopped_at=NULL WHERE id=?",(a,))   # force both live
check("41b DB no longer rejects two live rows in one window (app-level now)",
      len(live_in_window(c, SOCK, 5)) == 2,
      "reconcile's ground-truth rebuild keeps it to one; the reconcile fixture tests that")

# ── 41c. THE RESUME REGRESSION — the reason this whole pass exists ─────────
# Old model: die -> trigger live=0; resume (W2b sets stopped_at=NULL) never reset
# live, so job/window read as free forever while the session was live. Permanent
# desync. New model: liveness IS stopped_at, so revive is self-consistent with
# no flag to forget. This check FAILS on the old schema and passes on the new.
c = fresh()
pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
               " VALUES('u1','/code/herd','working',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
c.execute(W["W4_end"], {"session_id":"u1","now":T1})            # die (SessionEnd)
dead_job = c.execute(W["R_job_live"], {"job":"api"}).fetchone()
dead_win = live_in_window(c, SOCK, 5)
c.execute(W["W2b_insert"], {"session_id":"u1","cwd":"/code/herd","model":"opus",
                            "transcript":"/t.jsonl","now":T2})  # claude --resume
revived_job = c.execute(W["R_job_live"], {"job":"api"}).fetchone()
revived_win = live_in_window(c, SOCK, 5)
check("41c resume revives job+window with no stored flag to desync",
      dead_job is None and dead_win == []
      and revived_job is not None and revived_job["session_pk"] == pk and revived_win == [pk],
      f"dead(job={dead_job and dead_job['session_pk']},win={dead_win}) -> "
      f"revived(job={revived_job and revived_job['session_pk']},win={revived_win}); "
      "old model left both empty forever")

# ── 42. THE THESIS: last_event_at must advance on repeated same-status events ──
# post_tool_use.sh is the hot path and always sends status='working'. A guard of
# `AND status IS NOT :status` freezes last_event_at, and herd then pages you
# about a session that is actively running tools. klawde's bug, mirrored.
c = fresh()
c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
          " VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
for t in ("2026-07-15T10:01:00.000Z","2026-07-15T10:02:00.000Z",T1):
    c.execute(W["W4_event"], {"status":"working","now":t,"etype":"tool","session_id":"s1"})
    c.execute(W["W4_event_log"], {"etype":"tool","now":t,"raw":None,"session_id":"s1"})
r = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
check("42 last_event_at advances on repeated same-status events", r == T1,
      f"busy session must not read as silent; got {r}")
ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
check("42b events and sessions agree", ev == 3 and r == T1,
      "nothing reads events, so a disagreement here is invisible forever")

# ── 43. adoption must target the LIVE row ─────────────────────────────────
def reused_window():
    """dead session A and live session B, both claiming window 5."""
    c = fresh()
    a = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(111,'/code/herd',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'reconcile',?)",(a,T0,SOCK,T0))
    c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
    b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'reconcile',?)",(b,T2,SOCK,T2))
    return c, a, b

with guard("43 W2 adopts the LIVE row, not the dead predecessor"):
    c, a, b = reused_window()
    c.execute(W["W2_adopt"], {"session_id":"uuid-B","cwd":"/code/herd","model":"opus",
                              "transcript":"/t.jsonl","now":T2,"socket":SOCK,"win":5})
    dead_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(a,)).fetchone()[0]
    live_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(b,)).fetchone()[0]
    check("43 W2 adopts the LIVE row, not the dead predecessor",
          live_sid == "uuid-B" and dead_sid is None,
          f"dead={dead_sid} live={live_sid}")

# W5b (Path C) must be equally careful. Built on its OWN fixture: sharing 43's
# would make this assert nothing when 43 fails, and collide on sessions.session_id.
with guard("43b W5b statusline-adopt also targets the LIVE row"):
    c, a, b = reused_window()
    c.execute(W["W5b_adopt"], {"session_id":"uuid-C","now":T2,"socket":SOCK,"win":5})
    dead_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(a,)).fetchone()[0]
    live_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(b,)).fetchone()[0]
    check("43b W5b statusline-adopt also targets the LIVE row",
          live_sid == "uuid-C" and dead_sid is None,
          f"dead={dead_sid} live={live_sid}")

# ── 44. source-level: liveness is derived, never stored ───────────────────
# The invariant flipped with the decouple. It USED to be "every (socket,window)
# lookup carries AND live=1". It is now "liveness is read from sessions, never a
# herd_sessions.live column" — so any statement that looks up a session by
# (socket, window_id) must JOIN sessions and filter stopped_at, and the word
# `live` must not appear as a column reference anywhere.
writes_code = "\n".join(l.split("--")[0] for l in WRITES.splitlines())
stmts = [s for s in writes_code.split(";") if "window_id" in s and "kitty_socket" in s]
lookups = [s for s in stmts if re.search(r"WHERE[\s\S]*kitty_socket\s*=", s, re.I)]
offenders = [" ".join(s.split())[:70] for s in lookups
             if not re.search(r"stopped_at\s+IS\s+NULL", s, re.I)]
check("44 every (socket,window_id) lookup derives liveness via stopped_at",
      not offenders,
      f"un-joined: {offenders}" if offenders else "W2, W3a, W5b all JOIN sessions.stopped_at")
check("44a no `live` column reference survives in the write paths",
      not re.search(r"\blive\s*=\s*1\b", writes_code, re.I),
      "the denormalization is gone; nothing may filter on it")
# Ask the DB what the index actually IS — it must NOT be unique now.
c = fresh()
idx_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_herd_window'").fetchone()
check("44b idx_herd_window is a plain (non-unique) lookup index",
      idx_sql is not None and "unique" not in idx_sql[0].lower(),
      f"got: {idx_sql[0] if idx_sql else 'MISSING'}")
check("44b2 herd_sessions has no `live` column",
      not any(r[1] == "live" for r in c.execute("PRAGMA table_info(herd_sessions)")),
      "liveness lives only in sessions.stopped_at")
# and the inverse of 42, at source level: statusline may never carry a clock
w5 = W["W5_statusline"].split("--")[0]
check("44c W5_statusline never touches last_event_*", "last_event" not in w5.lower(),
      "the one invariant no runtime test survives someone 'optimizing' the statusline")

# ═══════════════════════════════════════════════════════════════════════════
# N. HOOKS — end to end, running the real scripts
#
# The hooks are bash and the SQL is a file, so nothing but this exercises them.
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ N. HOOKS (47-54) ═══\033[0m")
import subprocess, json, tempfile, shutil

HOOKS = pathlib.Path(__file__).resolve().parent / "src" / "herd" / "hooks"
RUNTIME = tempfile.mkdtemp(prefix="herd-validate-")
DBPATH = str(pathlib.Path("f.db").resolve())

def hook(script, payload, env=None):
    e = dict(os.environ)
    e.update({"HERD_DB": DBPATH, "HERD_RUNTIME": RUNTIME,
              "HERD_ERRLOG": f"{RUNTIME}/err.log"})
    if env: e.update(env)
    return subprocess.run(["bash", str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)

def bash_stmt(name):
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; stmt {name}'],
                       capture_output=True, text=True, env=dict(os.environ, HERD_DB=DBPATH))
    return r.stdout

def norm(s): return " ".join(s.split())

# ── 47. bash and python must extract the SAME statement ───────────────────
# This is what makes "writes.sql is canonical" true for BOTH runtimes. Without
# it, herd.db.load_statements() and the hooks' stmt() are two parsers of one
# file that agree only by luck — and every check above tests only the python one.
with guard("47 bash stmt() and python load_statements() agree"):
    mismatch = [n for n in W if norm(bash_stmt(n)) != norm(W[n])]
    check("47 bash stmt() and python load_statements() agree", not mismatch,
          f"drifted: {mismatch}" if mismatch else f"all {len(W)} statements identical in both runtimes")

# ── 48/49. session_start: adopt vs insert ─────────────────────────────────
SOCK = "unix:/tmp/kitty-20035"
with guard("48 session_start adopts the reconciled row via (socket,window_id)"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at)"
                   " VALUES('/code/herd','unknown','reconcile',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
              "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
    c.close()
    hook("session_start.sh", {"session_id":"uuid-A","cwd":"/code/herd","model":"claude-opus-4-8",
                              "transcript_path":"/t.jsonl","source":"startup",
                              "hook_event_name":"SessionStart"},
         {"KITTY_WINDOW_ID":"5","KITTY_LISTEN_ON":SOCK})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT id,session_id,status,model FROM sessions").fetchall()
    # job_name is the discriminator: W2b would create a BRAND NEW session row
    # with no herd_sessions attached. Only a true adopt leaves the spawn-time
    # job still bound to the row Claude's UUID landed on. Asserting on rowid
    # alone silently passes when the fallback fires and happens to reuse id 1.
    job = c.execute("SELECT h.job_name FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk"
                    " WHERE s.session_id='uuid-A'").fetchone()
    check("48 session_start adopts the reconciled row via (socket,window_id)",
          len(rows)==1 and rows[0]["id"]==pk and rows[0]["session_id"]=="uuid-A"
          and rows[0]["status"]=="working" and job is not None and job["job_name"]=="api",
          f"adopted pk={pk}, job still attached={job and job['job_name']}, {len(rows)} row(s) total")
    c.close()

with guard("49 session_start falls back to W2b outside kitty"):
    c = fresh(); c.close()
    hook("session_start.sh", {"session_id":"uuid-B","cwd":"/x","model":"claude-opus-4-8",
                              "transcript_path":"/t.jsonl","source":"startup",
                              "hook_event_name":"SessionStart"},
         {"KITTY_WINDOW_ID":"", "KITTY_LISTEN_ON":""})
    c = sqlite3.connect(DBPATH)
    n = c.execute("SELECT COUNT(*) FROM sessions WHERE session_id='uuid-B' AND status='working'").fetchone()[0]
    check("49 session_start falls back to W2b outside kitty", n==1,
          "hooks wired but herd never saw the window")
    c.close()

# ── 50. THE HOT PATH: last_event_at must advance, and throttle must not lie ──
with guard("50 post_tool_use advances last_event_at"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
              "started_at,updated_at) VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
    c.close()
    hook("post_tool_use.sh", {"session_id":"s1","tool_name":"Bash","tool_input":{},
                              "tool_response":"ok","hook_event_name":"PostToolUse"},
         {"HERD_TOOL_THROTTLE":"0"})
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("50 post_tool_use advances last_event_at", le != T0,
          f"a busy session must never read as silent; last_event_at={le}")
    c.close()

# NOTE the session_id differs from check 50's. The throttle state is a tmpfs
# file keyed by session_id, so reusing 's1' here would inherit check 50's
# just-written timestamp and suppress all five calls — the check would fail
# while the hook behaved perfectly.
with guard("50b throttle suppresses a burst but keeps the first write"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
              "started_at,updated_at) VALUES('s2','/a','working',?,'tool',?,?)",(T0,T0,T0))
    c.close()
    for _ in range(5):
        hook("post_tool_use.sh", {"session_id":"s2","tool_name":"Bash","tool_input":{},
                                  "tool_response":"ok","hook_event_name":"PostToolUse"},
             {"HERD_TOOL_THROTTLE":"60"})
    c = sqlite3.connect(DBPATH)
    ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s2'").fetchone()[0]
    check("50b throttle suppresses a burst but keeps the first write",
          ev == 1 and le != T0,
          f"5 tool calls -> {ev} write(s), last_event_at moved={le != T0}")
    c.close()

# ── 51. Stop — the 'waiting' signal klawde has no hook for ────────────────
with guard("51 stop sets waiting and re-arms attention"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
                   "started_at,updated_at) VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0)).lastrowid
    c.execute("INSERT INTO herd_attention(session_pk,attention_at,paged_at,paged_level,ack_at)"
              " VALUES(?,?,?,2,?)",(pk,T0,T0,T0))
    c.close()
    hook("stop.sh", {"session_id":"s1","stop_hook_active":False,
                     "last_assistant_message":"done","hook_event_name":"Stop"})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    r = c.execute("SELECT status,last_event_type FROM sessions WHERE session_id='s1'").fetchone()
    att = c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]
    check("51 stop sets waiting and re-arms attention",
          r["status"]=="waiting" and r["last_event_type"]=="stop" and att==0,
          f"status={r['status']} etype={r['last_event_type']} attention_rows={att}")
    c.close()

# ── 52. Notification filters to permission_prompt only ────────────────────
with guard("52 notification ignores idle_prompt, honours permission_prompt"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?)",(T0,T0))
    c.close()
    hook("notification.sh", {"session_id":"s1","notification_type":"idle_prompt",
                             "message":"Claude is waiting","hook_event_name":"Notification"})
    c = sqlite3.connect(DBPATH)
    after_idle = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    c.close()
    hook("notification.sh", {"session_id":"s1","notification_type":"permission_prompt",
                             "message":"allow?","hook_event_name":"Notification"})
    c = sqlite3.connect(DBPATH)
    after_perm = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("52 notification ignores idle_prompt, honours permission_prompt",
          after_idle=="working" and after_perm=="needs_approval",
          f"idle->{after_idle}, permission->{after_perm}; Stop owns 'waiting', not idle_prompt")
    c.close()

# ── 53. SessionEnd is the hook-driven death, and frees the handles ────────
# No trigger, no live flag: setting stopped_at is what makes R_job_live and the
# window JOIN see the session as dead. The slot frees automatically.
with guard("53 session_end stops the session and frees job+window"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                   " VALUES('s1','/a','working',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
              "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
    c.close()
    hook("session_end.sh", {"session_id":"s1","reason":"prompt_input_exit",
                            "hook_event_name":"SessionEnd"})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    r = c.execute("SELECT status,status_source,stopped_at FROM sessions WHERE session_id='s1'").fetchone()
    job_free = c.execute(W["R_job_live"], {"job":"api"}).fetchone() is None
    win_free = not c.execute("SELECT 1 FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                             "WHERE h.window_id=5 AND s.stopped_at IS NULL").fetchone()
    check("53 session_end stops the session and frees job+window",
          r["status"]=="stopped" and r["stopped_at"] is not None
          and r["status_source"]=="hook" and job_free and win_free,
          f"status_source={r['status_source']} (hook KNOWS; reconcile infers 'pid'); "
          f"job_free={job_free} win_free={win_free}")
    c.close()

# ── 54. bind() must refuse an unbound param, never bind NULL silently ─────
# sqlite3's own `.param set` fails exactly this way: mis-tokenize the value and
# the parameter is silently NULL. That is how you lose data quietly.
with guard("54 bind() refuses unbound params"):
    r = subprocess.run(["bash","-c",
        f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd WHERE session_id = :session_id;"'],
        capture_output=True, text=True, env=dict(os.environ, HERD_P_cwd="/a"))
    check("54 bind() refuses unbound params", r.returncode != 0 and ":session_id" in r.stderr,
          f"rc={r.returncode}, stderr mentions the missing param")

# ── 54b. the single-pass property, through the real bind() ───────────────
with guard("54b bind() does not rescan substituted values"):
    r = subprocess.run(["bash","-c",
        f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd, updated_at = :now;"'],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_P_cwd="/tmp/:now/x", HERD_P_now="T1"))
    check("54b bind() does not rescan substituted values",
          "'/tmp/:now/x'" in r.stdout, f"a cwd containing ':now' survives: {r.stdout.strip()}")

# ── 55. stop.sh re-arms through writes.sql (W6d_rearm_sid), end to end ────
with guard("55 stop re-arm goes through the canonical statement"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                   " VALUES('s9','/a','working',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_attention(session_pk,attention_at,paged_level) VALUES(?,?,3)",(pk,T0))
    c.close()
    hook("stop.sh", {"session_id":"s9","stop_hook_active":False,"hook_event_name":"Stop"})
    c = sqlite3.connect(DBPATH)
    att = c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]
    check("55 stop re-arm goes through the canonical statement", att==0,
          "attention row cleared by W6d_rearm_sid, not by inlined SQL")
    c.close()

# ── 56. NO hook may inline DML. Every write goes through run()/writes.sql, or
# the check-47 drift guard is blind to it and bind()'s quoting is bypassed. The
# one exception is common.sh's db()/run() plumbing, which is the sanctioned path.
with guard("56 no hook script inlines INSERT/UPDATE/DELETE"):
    offenders = []
    for shf in sorted(HOOKS.glob("*.sh")):
        if shf.name == "common.sh":
            continue   # the db()/run() wrapper IS the sanctioned SQL path
        for i, line in enumerate(shf.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\b(INSERT|UPDATE|DELETE)\s", code, re.I):
                offenders.append(f"{shf.name}:{i}")
    check("56 no hook script inlines INSERT/UPDATE/DELETE", not offenders,
          f"inlined DML at {offenders}" if offenders
          else "every write routes through run() -> writes.sql")

# ── 57. run_tx is ATOMIC: a mid-transaction failure leaves NOTHING written ──
# This is the property -bail buys. Without it the sqlite3 CLI runs UPDATE, skips
# the failing INSERT, and COMMITs the UPDATE anyway — a half-write. The check
# forces a failure by pointing the second statement's FK at a missing row, and
# asserts the first statement's effect did not survive.
with guard("57 run_tx rolls back the whole transaction on a mid-tx failure"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?,?)",(T0,T0,T0))
    c.close()
    # W4_event (updates s1, succeeds) + a second statement that violates the FK.
    r = subprocess.run(["bash","-c",
        '. "$1/common.sh"; '
        'export HERD_P_session_id=s1 HERD_P_now=T9 HERD_P_status=working '
        'HERD_P_etype=tool HERD_P_raw=""; '
        # hand a bad statement to run_tx by shadowing stmt for one name:
        'run_tx W4_event BOGUS_FK', "_", str(HOOKS.parent)],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_DB=DBPATH, HERD_RUNTIME=RUNTIME,
                 HERD_ERRLOG=f"{RUNTIME}/err.log"))
    # BOGUS_FK doesn't exist -> run_tx aborts before executing anything, so
    # last_event_at must still be T0. (Belt: even if it HAD run W4_event alone,
    # the missing-statement guard returns before the db call.)
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("57 run_tx rolls back the whole transaction on a mid-tx failure",
          le == T0, f"an unknown statement must abort with NOTHING written; last_event_at={le}")
    c.close()

# ── 57b. the real atomicity path: a runtime FK error inside the tx ────────
with guard("57b -bail rolls back a committed-prefix on runtime error"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?,?)",(T0,T0,T0))
    c.close()
    # Build a two-statement tx by hand through db(): a valid UPDATE then an
    # INSERT with a bad FK. Mirrors exactly what run_tx emits.
    tx = ("BEGIN IMMEDIATE;\n"
          "UPDATE sessions SET last_event_at='T9' WHERE session_id='s1';\n"
          "INSERT INTO events(session_pk,event_type,source,timestamp) "
          "VALUES(99999,'x','hook','T9');\n"
          "COMMIT;\n")
    subprocess.run(["bash","-c", f'. "{HOOKS}/common.sh"; db', "_"],
                   input=tx, capture_output=True, text=True,
                   env=dict(os.environ, HERD_DB=DBPATH, HERD_RUNTIME=RUNTIME,
                            HERD_ERRLOG=f"{RUNTIME}/err.log"))
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("57b -bail rolls back a committed-prefix on runtime error",
          le == T0 and ev == 0,
          f"the UPDATE must NOT survive the failed INSERT; last_event_at={le}, events={ev}")
    c.close()

shutil.rmtree(RUNTIME, ignore_errors=True)

print("\n" + "═"*72)
if FAILED:
    print(f"\033[31m{len(FAILED)} FAILED:\033[0m " + ", ".join(FAILED)); sys.exit(1)
print("\033[32mALL CHECKS PASS — foundation validated\033[0m")
