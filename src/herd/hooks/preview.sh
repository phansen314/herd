#!/bin/bash
# herd preview pane — detail for ONE session id, on stdout.
#
# NOT A CLAUDE HOOK. Nothing in settings.json runs this; it lives in hooks/ for
# common.sh (stmt/db + the HERD_* defaults) and because install.py's copy, drift
# and +x checks all glob hooks/*.sh, so it ships and stays current for free.
#
# WHY BASH. fzf runs --preview through `sh -c` on EVERY highlight change, and most
# of a python verb's cost here is bare interpreter startup. See DESIGN.md#preview.
#
# BYTE-FOR-BYTE TWIN of cli._preview_text(). The duplication is deliberate and
# pinned: tests/test_preview_bash.py asserts this script's stdout equals that
# function's output for every row shape. Change one, change both.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

# Mirror cmd_preview's guard: a non-numeric id is a caller bug, not a dead session.
case "$1" in ''|*[!0-9]*) exit 1 ;; esac

SQL=$(stmt R1_list)
if [ -z "$SQL" ]; then
    herd_log "no such statement: R1_list"
    echo "(session gone)"
    exit 0
fi

# \x1f fields (tab is IFS whitespace and collapses the empty fields we need),
# \x1e ROWS — NOT newline: session_name is arbitrary /rename text and cwd is a
# path, either of which may contain a newline, which under newline-delimited rows
# would shift every later row's fields and render ANOTHER session into the pane.
# A wrong preview is worse than a blank one, so the parse also fails closed on
# NF != 20 (which absorbs the trailing empty record sqlite3 leaves).
printf '.mode list\n.separator "%s" "%s"\n%s\n' $'\037' $'\036' "$SQL" | db | awk \
    -v RS=$'\036' -v FS=$'\037' -v want="$1" '
    function g(v) { return v == "" ? "—" : v }
    BEGIN {
        mark["waiting"] = "🙋"; reason["waiting"] = "waiting for you"
        mark["needs_approval"] = "🔐"; reason["needs_approval"] = "needs approval"
        mark["working"] = "🥱"; reason["working"] = "stuck — no activity"
    }
    # Wrong field count means a VALUE contained \x1f or \x1e. Skip it, but COUNT it:
    # otherwise an unparseable row is indistinguishable from a dead one and the pane
    # says "(session gone)" about a live session. Count only OUR row — the id is
    # field 1 and corruption is always later, so $1 still identifies the record.
    NF != 20 { if ($1 == want) bad++; next }
    $1 != want { next }
    {
        id=$1; sid=$2; pid=$3; cwd=$4; status=$5; ssrc=$6; model=$7; sname=$8
        ctxp=$9; cost=$10; branch=$11; levt=$12; ltyp=$13; started=$14
        job=$16; att=$19; ack=$20

        name = sname
        if (name == "") name = job
        if (name == "") name = substr(sid, 1, 8)
        if (name == "") name = "—"

        # `is not None` on the python side, NOT truthiness: 0 renders, NULL does not.
        ctx  = (ctxp == "") ? "—" : ctxp "%"
        cst  = (cost == "") ? "—" : sprintf("$%.2f", cost)

        printf "name      %s\n", name
        printf "session   %s\n", g(sid)
        printf "herd id   #%s\n", id
        printf "status    %s%s\n", g(status), (ssrc == "" ? "" : "  (" ssrc ")")
        printf "model     %s\n", g(model)
        printf "job       %s\n", g(job)
        printf "pid       %s\n", g(pid)
        printf "cwd       %s\n", g(cwd)
        printf "branch    %s\n", g(branch)
        printf "context   %s\n", ctx
        printf "cost      %s\n", cst
        printf "started   %s\n", g(started)
        printf "last      %s  (%s)\n", g(levt), g(ltyp)
        if (att != "" && ack == "") {       # acked -> armed but quiet
            m = (status in mark) ? mark[status] : "❗"
            r = (status in reason) ? reason[status] : "needs attention"
            printf "%s %s since %s\n", m, r, att
        }
        found = 1
        exit
    }
    # "gone" is a claim about the session. Only make it when the data was readable.
    END {
        if (!found)
            print bad ? "(preview unavailable — unreadable row)" : "(session gone)"
    }
'
exit 0
