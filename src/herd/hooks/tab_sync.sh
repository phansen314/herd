#!/bin/bash
# UserPromptSubmit — capture the live kitty TAB title into tier-2 (herd_sessions).
# A DEDICATED enrichment hook: the tier-1 lifecycle hooks (session_start.sh, stop.sh,
# …) stay free of any kitten fork or kitty dependency; this one script owns live
# kitty-state capture. tab_title is the ONE piece of kitty render state herd persists,
# because a DEAD session can't be re-derived from `kitten @ ls` and restart needs its
# real title. See DESIGN.md#restart.
#
# MUST EXIT 0: async, and a nonzero would surface on the prompt. Best-effort — any
# missing tool / not-in-kitty / kitten failure just skips the write, never fails.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

read_input
payload_read '.session_id' SID
valid_sid "$SID" || exit 0

# Nothing to capture outside kitty, or with remote control off (no socket). The
# window id goes to jq as a NUMBER (--argjson), so it must be all-digits.
[ -n "${KITTY_LISTEN_ON:-}" ] || exit 0
case "${KITTY_WINDOW_ID:-}" in ''|*[!0-9]*) exit 0 ;; esac
command -v kitten >/dev/null 2>&1 || exit 0

# Bound the round trip: a stale unix:/tmp/kitty-<pid> socket blocks forever (focus.py
# bounds its own `kitten @ ls` the same way). `timeout` is coreutils — skip the bound
# where it is absent (macOS) rather than fail; the hook is async and exits 0 anyway.
TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout 5"
# kitten @ ls -> [os-window].tabs[].{title, windows[].id}. The tab whose windows
# include THIS window is this session's tab. first(...) // empty -> one line, empty
# on miss. flatten_windows (focus.py) discards this tab level; there is no reusable
# helper, so the lookup is done here.
title=$($TO kitten @ ls --to "$KITTY_LISTEN_ON" 2>/dev/null \
        | jq -r --argjson wid "$KITTY_WINDOW_ID" \
            'first(.[].tabs[] | select(any(.windows[]; .id == $wid)) | .title) // empty' \
        2>/dev/null)
[ -n "$title" ] || exit 0

# Tier-2 only, through the named statement (never inline DML). No row for this session
# (outside kitty at start) -> W7_tab_title matches nothing; harmless.
export HERD_P_session_id="$SID" HERD_P_tab_title="$title"
run W7_tab_title >/dev/null 2>&1
exit 0
