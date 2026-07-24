# bash completion for herd — lazy-loaded from ~/.local/share/bash-completion/completions/herd
# Candidates are DATA — job names, /rename names, cwd fragments — so the unquoted
# array assignment `COMPREPLY=( $(compgen ...) )` was wrong twice: it word-split a
# name containing a space into two candidates, and it glob-expanded one containing
# `*` against the current directory. IFS=$'\n' splits on lines only; set -f turns
# off pathname expansion for the assignment, and both are restored after.
_herd_offer() {
    local cur="$1" words="$2" oldifs="$IFS" noglob=0
    case $- in *f*) noglob=1 ;; esac
    set -f
    IFS=$'\n'
    COMPREPLY=( $(compgen -W "$words" -- "$cur") )
    IFS="$oldifs"
    [ "$noglob" -eq 1 ] || set +f
}

_herd_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    local prev=${COMP_WORDS[COMP_CWORD-1]}
    if [ "$COMP_CWORD" -eq 1 ]; then
        _herd_offer "$cur" "ls jump spawn watch restart doctor"
    elif [ "${COMP_WORDS[1]}" = jump ]; then
        _herd_offer "$cur" "$(herd complete 2>/dev/null)"
    elif [ "${COMP_WORDS[1]}" = spawn ] && { [ "$prev" = -t ] || [ "$prev" = --template ]; }; then
        _herd_offer "$cur" "$(herd tcomplete 2>/dev/null)"
    fi
}
complete -F _herd_complete herd
