# herd

Track your Claude Code sessions in a local SQLite database — which are working,
which are waiting on *you*, where they live in [kitty](https://sw.kovidgoyal.net/kitty/),
and what they've cost — so a pile of sessions across a pile of terminal windows
stops being something you have to hold in your head.

herd is **local-only**: no network, no telemetry, no runtime dependencies. Hooks
are bash + `jq` + `sqlite3` on purpose (a Python cold start on every `SessionStart`
would be felt); the background daemon is stdlib Python.

> **Status: early but usable.** The data model, lifecycle hooks, statusline, the
> liveness/attention daemon, and the `herd` CLI (`ls`, `spawn`, fzf-powered `jump`,
> and the `watch` dashboard) are built, tested, and installed. Still ahead (see
> [Roadmap](#roadmap)): a lightweight notifier for ambient attention.

## What it does

Every Claude Code session writes into `~/.herd/herd.db` as it lives:

- **Identity & state** — session UUID, cwd, model, and Claude's own status:
  `working`, `waiting` (turn ended, wants input), `needs_approval` (permission
  prompt), `stopped`.
- **Placement** — the kitty socket + window it's running in (the jump target).
- **Metrics** — context %, cost, burn rate, rate-limit windows (from the statusline).
- **Liveness** — a background daemon reaps sessions whose process died *silently*
  (kill -9, crash, closed terminal) where no hook could fire.
- **Attention** — the daemon derives which sessions need you (waiting too long,
  a permission prompt sitting, a working session gone silent) and records it.

## How it works

Two data sources, two directions:

- **Hooks (push).** Claude Code fires lifecycle hooks — `SessionStart`, `Stop`,
  `Notification`, `PostToolUse`, `SessionEnd` — plus the statusline (~1/sec). They
  record what Claude *reports*: identity, status, metrics, and (from the kitty
  environment) placement. `SessionStart` also walks the process tree to capture
  Claude's own pid.
- **Daemon (pull).** A background process reads the **process table** each tick to
  catch what hooks structurally cannot: a session that died without firing
  `SessionEnd`. Liveness comes from `ps`, never from kitty — absence from a kitty
  listing is evidence about *placement*, and reaping on it would nuke every live
  row on a socket blip.

### Two tiers

The schema is split along a strict boundary (enforced by the test suite):

- **Tier 1 — `sessions`, `events`** — facts that would be true whether or not herd
  existed: a Claude process with this pid, cwd, status. Core session data.
- **Tier 2 — `herd_sessions`, `herd_attention`** — herd's *relationship* to a
  session: the job name it was spawned with, its kitty placement, and whether
  herd has decided it needs your attention.

The identity spine is a surrogate integer `id`; Claude's UUID is a nullable column
adopted later — which is what lets a session have a placement and a job *before*
Claude has reported its UUID.

### The daemon's two layers

`herd.daemon` runs both on one loop, mirroring the tier boundary:

| layer | writes | when |
|---|---|---|
| **core** (tier 1) | `sessions.stopped_at` via `ps` liveness — the reaper | always |
| **herd** (tier 2) | `herd_attention` — the silence rule | gated by `HERD_ATTENTION` |

Run herd purely for **core data collection** with `HERD_ATTENTION=0` and build your
own tooling on the `sessions` table; herd never touches `herd_attention`.

If you do read the DB from your own tool, treat `sessions.status` as an **open set**:
render any value you don't recognize as `unknown` rather than switching exhaustively.
The current values are `working`, `waiting`, `needs_approval`, `stopped`, `unknown`,
and the `CHECK` constraint that enforces them will gain members as Claude Code adds
lifecycle hooks. Growing that set is additive for readers that degrade gracefully and
breaking for ones that don't. Same rule for `last_event_type` and `status_source`.

### Canonical SQL

Every write is a named statement in [`schema/writes.sql`](src/herd/schema/writes.sql).
Both the bash hooks and the Python daemon load statements from that one file — SQL
is never inlined into a hook or the daemon. The test suite proves the bash and Python
extractors return character-for-character identical statements, so a fixed bug can't
quietly rot in a copy.

## Install

Requires: `bash`, `jq`, `sqlite3`, Python ≥ 3.9, and — for `jump`/`watch`/placement —
`fzf` and kitty. herd is run from the source tree — no `pip install` needed.

An existing `~/.claude/settings.json` is expected; the installer edits it in place.

```bash
git clone <repo> ~/code/herd && cd ~/code/herd
PYTHONPATH=src python3 -m herd.install            # wire everything
PYTHONPATH=src python3 -m herd.install --dry-run  # preview, touch nothing
```

The installer:

1. **bootstraps** `~/.herd/herd.db`;
2. **wires the hooks + statusline** into `~/.claude/settings.json` and the statusline
   wrapper — backing up each file first (`*.herd-bak.<ts>`) and preserving any hooks
   it doesn't own (e.g. an existing PreToolUse hook);
3. **installs the daemon** as a `systemd --user` service (`herd.service`), enabled on
   login with auto-restart. Where `systemctl --user` is unavailable (macOS/headless)
   this step is a graceful no-op — run the daemon yourself.
4. **symlinks the CLI** — `herd` into `~/.local/bin` and bash completion into
   `~/.local/share/bash-completion/completions` (WARNs if `~/.local/bin` isn't on
   your PATH);
5. **self-tests** — runs the wired hooks against a temp DB and prints PASS/FAIL.

If you use [klawde](https://github.com/wolffiex/klawde), note that the installer
**unwires it**: any hook command under `/.klawde/` is dropped from `settings.json`
(the two tools both own the statusline and would fight).

Undo it — hooks, statusline, service, and the CLI symlinks — with:

```bash
PYTHONPATH=src python3 -m herd.install --uninstall
```

This works by **restoring the most recent `*.herd-bak.<ts>` backup** of each file it
edited, not by reversing the edits — if those backups are gone it says so and leaves
the file wired, and you unwire `~/.claude/settings.json` by hand. Your data survives
either way: `~/.herd/herd.db` is never deleted.

## Using it

**The `herd` CLI** (installed on your PATH):

```bash
herd ls                 # live sessions, attention-first, by name
herd spawn <job>        # launch claude in a new kitty tab, tracked from the start
herd jump               # fuzzy-pick a session (fzf) with a live preview, and focus it
herd jump <query>       # herd id, name (/rename), job, uuid, or cwd; unique match jumps
herd watch              # the picker as a permanent dashboard, for a dedicated tab
```

**`herd spawn`** names a session up front, so it has a handle before Claude has even
reported a UUID:

```bash
herd spawn api                              # a new tab, cwd here
herd spawn api --pane                       # a split instead
herd spawn api --cwd ~/code/x --prompt "review the diff" -- --model opus
```

Job names are recyclable: `spawn` refuses a name a *live* session already holds, but
once that session dies the name is free again. Everything after `--` is passed
through to `claude`.

Sessions show by their recognizable name — Claude's `/rename` name, else herd's job,
else the uuid. `jump` opens an fzf picker (with a detail preview) unless the query is
a unique match, in which case it focuses immediately.

**`herd watch`** is the dashboard: the same picker, looping, refreshing itself as
sessions change. Give it a dedicated kitty tab and a key to reach it:

```conf
# ~/.config/kitty/kitty.conf — focus the herd tab, launching it once if needed.
# --allow-remote-control is REQUIRED: a plain --type=background process gets no
# KITTY_LISTEN_ON, so every `kitten @` in the script fails silently and the key
# looks like it does nothing.
map ctrl+space>c launch --type=background --allow-remote-control ~/.config/kitty/focus-herd.sh
```

```sh
#!/bin/sh
# ~/.config/kitty/focus-herd.sh
set -eu
# Match values are unanchored regexes — bare `title:herd` also hits `herd-2`.
kitten @ focus-tab --match 'title:^herd$' 2>/dev/null && exit 0
exec kitten @ launch --type=tab --tab-title herd --cwd "$HOME" \
    bash -l -i -c 'herd watch; exec bash'
```

The login shell matters: `herd` lives in `~/.local/bin`, which a bare `launch` may not
have on PATH. `exec bash` means quitting the dashboard leaves a shell rather than
closing the tab.

To have the tab from the start, add it to your `startup_session` file:

```conf
new_tab herd
cd ~
launch bash -l -i -c 'herd watch; exec bash'
```

Enter jumps to a session, `ctrl-r` forces a refresh, `ctrl-q` / `ctrl-c` quit. Esc
re-opens the picker rather than exiting — it's a tab you can't accidentally fall out
of, so after jumping away, the same key brings you back to a live list.

**Or read the DB directly** — everything the CLI shows, and more:

```bash
sqlite3 -header -column ~/.herd/herd.db "
SELECT s.id, substr(s.session_id,1,8) uuid, s.pid, s.status,
       h.job_name job, h.window_id win, s.context_percent ctx,
       printf('\$%.2f', s.total_cost_usd) cost,
       (a.attention_at IS NOT NULL) attn, s.cwd
FROM sessions s
LEFT JOIN herd_sessions  h ON h.session_pk = s.id
LEFT JOIN herd_attention a ON a.session_pk = s.id
WHERE s.stopped_at IS NULL
ORDER BY a.attention_at IS NULL, a.attention_at, s.started_at DESC;"
```

**The daemon:**

```bash
systemctl --user status herd            # is it running
systemctl --user restart herd           # after editing the source
journalctl --user -u herd -f            # watch it (quiet unless it errors)

# or run it by hand:
PYTHONPATH=src python3 -m herd.daemon           # reaper + attention
PYTHONPATH=src python3 -m herd.daemon --once    # a single tick
HERD_ATTENTION=0 PYTHONPATH=src python3 -m herd.daemon   # core-only
```

**Tuning the attention rule** (env vars, defaults shown):

| var | default | meaning |
|---|---|---|
| `HERD_ATTENTION` | `1` | `0`/`off` → core-only (reaper only, no `herd_attention`) |
| `HERD_WAIT_SECS` | `30` | grace before a `waiting` session needs you |
| `HERD_APPROVAL_SECS` | `15` | grace before a `needs_approval` prompt does |
| `HERD_STUCK_SECS` | `300` | silence before a `working` session reads as stuck |
| `HERD_DB` | `~/.herd/herd.db` | database path |

## Notifications (kitty tab bell)

herd sends no notifications itself — **Claude Code rings the bell, kitty flags the
tab.** For an ambient "this session wants you" marker, set Claude's notification
channel to the terminal bell in `~/.claude/settings.json`:

```json
"preferredNotifChannel": "terminal_bell"
```

The installer **offers** to set this (interactive, opt-in — it never forces it, and
never overrides a channel you've already chosen).

Claude rings it when a turn ends waiting for input or a permission prompt appears
(it's the only process with the window's tty — herd's hooks run detached, so they
can't). kitty then marks that tab and flags the window via `bell_on_tab` and
`window_alert_on_bell` (both on by default; add `enable_audio_bell no` for a silent,
visual-only bell), clearing when you focus the tab.

This is deliberately outside herd — the daemon stays kitty-free and you keep control
of your own Claude notification preference. herd's silence-rule signal (a session
gone quiet) shows separately as the `!` in `herd ls` / the jump picker.

## Development

The whole design is asserted, not narrated, by the `pytest` suite:

```bash
python3 -m pytest       # whole suite, no install needed, a few seconds
```

It runs the real bash hooks and the real Python against throwaway databases, and
proves the invariants the design rests on — the tier boundary, the identity model,
the two-clocks attention thesis, the reaper's liveness rules, and that the hooks and
daemon load the same canonical SQL. New behavior is added test-first (red before
green); the suite is the project's only CI gate.

The design rationale lives in [`DESIGN.md`](DESIGN.md); source comments carry a
one-line summary and point there. Start with the schema —
[`schema/core.sql`](src/herd/schema/core.sql) (tier 1),
[`schema/herd.sql`](src/herd/schema/herd.sql) (tier 2), and
[`schema/writes.sql`](src/herd/schema/writes.sql) (every write path).

## Layout

```
src/herd/
  schema/        core.sql · herd.sql · writes.sql   — the data model + canonical SQL
  hooks/         *.sh (bash 3.2 + jq + sqlite3)      — lifecycle capture + statusline
  db.py          statement loader + connection policy
  daemon.py      the reaper + attention tick
  install.py     hooks/statusline/service/CLI wiring
  cli.py         the `herd` CLI (ls, spawn, jump, watch + fzf preview machinery)
  spawn.py       `herd spawn` — SpawnSpec, the guards, and the W1 placeholder write
  kitty/         focus.py — re-derive a session's window and jump to it
                 launch.py — `kitten @ launch` for `herd spawn`
completions/     bash completion   ·   bin/herd — the CLI wrapper
tests/           pytest suite (helpers.py + conftest.py + test_*.py per concern)
DESIGN.md        the design rationale
```

## Roadmap

Navigation is the CLI (`herd jump` fuzzy-picks and focuses, `herd watch` keeps that
picker up as a dashboard), and ambient attention is Claude's terminal bell + kitty's
tab flag (see [Notifications](#notifications-kitty-tab-bell)) — so a dedicated TUI and
a herd-owned notifier are **not planned**; each is handled more cheaply outside herd.
fzf *is* the TUI: it already lists, navigates, and previews live. What's left:

- **More CLI verbs** as needed (`herd kill`, `herd dismiss`), each composing with
  `herd jump`'s fzf picker.
- *(maybe)* a daemon tab-poke for the one case Claude's bell can't cover — a session
  gone **silently stuck** in `working` (it isn't "done", so it never bells). This is
  the only thing that would put kitty back on the daemon's path, so it stays opt-in.

## Prior art

herd started as a rewrite of *klawde* and deliberately diverges from it: liveness is
derived from `stopped_at` rather than a denormalized flag; the idle signal is the gap
between two clocks rather than a single constantly-stamped one; and every write goes
through canonical SQL rather than statements re-typed into each hook.
