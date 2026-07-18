# bash completion for herd — lazy-loaded from ~/.local/share/bash-completion/completions/herd
_herd_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "ls jump preview" -- "$cur") )
    elif [ "${COMP_WORDS[1]}" = jump ] || [ "${COMP_WORDS[1]}" = preview ]; then
        COMPREPLY=( $(compgen -W "$(herd complete 2>/dev/null)" -- "$cur") )
    fi
}
complete -F _herd_complete herd
