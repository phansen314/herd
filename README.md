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
- **Metrics** — context %, cost, burn rate, token counts, lines changed, rate-limit
  windows (from the statusline).
- **Liveness** — a background daemon reaps sessions whose process died *silently*
  (kill -9, crash, closed terminal) where no hook could fire.
- **Attention** — the daemon derives which sessions need you (waiting too long,
  a permission prompt sitting, a working session gone silent) and records it.

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

1. **bootstraps** `~/.herd/herd.db` and `~/.herd/templates/`;
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

```bash
herd ls                 # live sessions, attention-first, by name
herd spawn <job>        # launch claude in a new kitty tab, tracked from the start
herd jump               # fuzzy-pick a session (fzf) with a live preview, and focus it
herd jump <query>       # herd id, name (/rename), job, uuid, or cwd; unique match jumps
herd watch              # the picker as a permanent dashboard, for a dedicated tab
```

Sessions show by their recognizable name — Claude's `/rename` name, else herd's job,
else the uuid. `jump` opens an fzf picker (with a detail preview) unless the query is
a unique match, in which case it focuses immediately.

### `herd spawn` — name a session up front

So it has a handle before Claude has even reported a UUID:

```bash
herd spawn api                              # a new tab, cwd here
herd spawn api --pane                       # a split instead
herd spawn api --cwd ~/code/x --prompt "review the diff" -- --model opus
```

Job names are recyclable: `spawn` refuses a name a *live* session already holds, but
once that session dies the name is free again. Everything after `--` is passed
through to `claude`.

**Templates** — a preset for a spawn you run often. Drop a TOML file in
`~/.herd/templates/` and pass `-t`:

```toml
# ~/.herd/templates/review.toml
job    = "review"          # optional — then `herd spawn -t review` needs no name
cwd    = "~/code/herd"
type   = "pane"            # tab | pane
title  = "code review"
args   = ["--model", "opus"]
prompt = """
Review the working diff.
Focus on correctness; skip style.
"""
```

```bash
herd spawn -t review                 # everything from the template
herd spawn api -t review --tab       # CLI wins: job=api, type=tab, rest from template
```

Precedence is **CLI flag > template > built-in default**, so a template is a set of
defaults you can always override. The one exception is `--`: those args are *appended*
to the template's `args` rather than replacing them, so a template can carry a base
set and you add ad-hoc flags on top. `-t` tab-completes.

A multiline `prompt` is the reason the format is TOML. Templates need **Python 3.11+**
(stdlib `tomllib`); the rest of herd still runs on 3.9. Unknown keys are rejected
rather than ignored, so a typo tells you instead of silently doing nothing.

### `herd watch` — the dashboard

The same picker, looping, refreshing itself as sessions change. Give it a dedicated
kitty tab and a key to reach it:

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

### Reading the database directly

Everything the CLI shows, and more. The DB lives at `~/.herd/herd.db` (WAL mode, so
`-wal` / `-shm` siblings exist — copy all three, or use `sqlite3 .backup`, if you
back it up):

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

The schema is split in two tiers — `sessions`/`events` are facts that would be true
whether or not herd existed; `herd_sessions`/`herd_attention` are herd's own
relationship to a session. Read tier 1 and you can ignore that herd exists at all.
(Rationale: [DESIGN.md](DESIGN.md#tiers).)

If you build a tool on this, treat `sessions.status` as an **open set**: render any
value you don't recognize as `unknown` rather than switching exhaustively. The current
values are `working`, `waiting`, `needs_approval`, `stopped`, `unknown`, and the
`CHECK` constraint that enforces them will gain members as Claude Code adds lifecycle
hooks. Growing that set is additive for readers that degrade gracefully and breaking
for ones that don't. Same rule for `last_event_type` and `status_source`.

### The daemon

```bash
systemctl --user status herd            # is it running
systemctl --user restart herd           # after editing the source
journalctl --user -u herd -f            # watch it (quiet unless it errors)

# or run it by hand:
PYTHONPATH=src python3 -m herd.daemon           # reaper + attention
PYTHONPATH=src python3 -m herd.daemon --once    # a single tick
HERD_ATTENTION=0 PYTHONPATH=src python3 -m herd.daemon   # core-only
```

It runs two layers on one loop, mirroring the tier boundary:

| layer | writes | when |
|---|---|---|
| **core** (tier 1) | `sessions.stopped_at` via `ps` liveness — the reaper | always |
| **herd** (tier 2) | `herd_attention` — the silence rule | gated by `HERD_ATTENTION` |

Run herd purely for **core data collection** with `HERD_ATTENTION=0` and build your
own tooling on the `sessions` table; herd never touches `herd_attention`.

**Tuning the attention rule** (env vars, defaults shown):

| var | default | meaning |
|---|---|---|
| `HERD_ATTENTION` | `1` | `0`/`off` → core-only (reaper only, no `herd_attention`) |
| `HERD_WAIT_SECS` | `30` | grace before a `waiting` session needs you |
| `HERD_APPROVAL_SECS` | `15` | grace before a `needs_approval` prompt does |
| `HERD_STUCK_SECS` | `300` | silence before a `working` session reads as stuck |
| `HERD_DB` | `~/.herd/herd.db` | database path |

Read by the hooks and CLI rather than the daemon:

| var | default | meaning |
|---|---|---|
| `HERD_TEMPLATES` | `~/.herd/templates` | where `herd spawn -t` looks for `<name>.toml` |
| `HERD_TOOL_THROTTLE` | `2` | seconds to coalesce `PostToolUse` writes on the hot path |
| `HERD_CLAUDE_NAME` | `claude` | process name the pid ancestry walk looks for (node-based installs) |
| `HERD_ERRLOG` | `~/.herd/hook-errors.log` | where hooks log failures (they never print to Claude) |

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

## Troubleshooting

Hooks **never** print to Claude — that's the contract, so they fail silently by
design. Errors go to `~/.herd/hook-errors.log` (`HERD_ERRLOG`); the daemon's go to
`journalctl --user -u herd`. Check those first.

| symptom | cause |
|---|---|
| `herd: command not found` | `~/.local/bin` not on PATH (the installer WARNs about this), or an open shell that hasn't rehashed — `hash -r` |
| statusline blank or missing | the hook scripts lost `+x`. `python3 -m herd.install` re-runs the selftest, which reports exactly this |
| no sessions ever appear | the daemon isn't running (`systemctl --user status herd`), or `~/.claude/settings.json` wasn't rewired — re-run the installer |
| sessions appear but never go away | the daemon is down; only it reaps silent deaths. Live rows are `stopped_at IS NULL` |
| `herd spawn` → "needs to run inside kitty" | `KITTY_LISTEN_ON` is unset. kitty needs `allow_remote_control yes` **and** `listen_on unix:/tmp/kitty` |
| `herd spawn -t` → "templates need Python 3.11+" | `tomllib` is 3.11+. Only templates need it; the rest of herd runs on 3.9 |
| `herd watch` → "needs fzf and a tty" | `fzf` isn't installed, or you're not on a terminal |
| `herd jump` prints a list instead of jumping | same — no `fzf`, so it degrades to printing rather than failing |
| a key bound to `launch --type=background` does nothing | that process gets no `KITTY_LISTEN_ON`; add `--allow-remote-control` (see [watch](#herd-watch--the-dashboard)) |
| no `systemctl --user` (macOS/headless) | expected — the service step no-ops. Run `python3 -m herd.daemon` yourself |

Start fresh: stop the daemon, delete `~/.herd/herd.db*` (the DB plus its `-wal` and
`-shm` siblings), and re-run the installer. You lose history; nothing else depends
on it.

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

Everything else — the tier boundary, the identity spine, the two clocks behind the
attention signal, and why every write is a named statement in one canonical SQL file
— is in [DESIGN.md](DESIGN.md).

## Development

The whole design is asserted, not narrated, by the `pytest` suite:

```bash
python3 -m pytest       # whole suite, no install needed, a few seconds
```

It runs the real bash hooks and the real Python against throwaway databases, and
proves the invariants the design rests on — the tier boundary, the identity model,
the two-clocks attention thesis, the reaper's liveness rules, that every hook exits 0
under every degradation, and that the hooks and daemon load the same canonical SQL.
New behavior is added test-first (red before green); the suite is the project's only
CI gate.

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
  spawn.py       `herd spawn` — SpawnSpec, resolve_spec, reserve-then-launch
  template.py    ~/.herd/templates/*.toml -> SpawnSpec defaults (herd spawn -t)
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
