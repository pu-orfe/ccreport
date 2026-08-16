# Operator-visible output. stdout is for the plan; stderr is only used for warnings and errors.

typeset -g CCR_STEP_TOTAL=0
typeset -g CCR_STEP_INDEX=0

ccr_info()    { print -r -- "${CCR_C_CYAN}·${CCR_C_RESET} $*"; }
ccr_ok()      { print -r -- "${CCR_C_GREEN}✓${CCR_C_RESET} $*"; }
ccr_warn()    { print -r -- "${CCR_C_YELLOW}!${CCR_C_RESET} $*" >&2; }
ccr_err()     { print -r -- "${CCR_C_RED}✗${CCR_C_RESET} $*" >&2; }
ccr_dim()     { print -r -- "${CCR_C_GREY}$*${CCR_C_RESET}"; }
ccr_blank()   { print -r -- ""; }
ccr_die()     { ccr_err "$*"; exit 1; }

ccr_heading() {
  ccr_blank
  print -r -- "${CCR_C_BOLD}$*${CCR_C_RESET}"
  print -r -- "${CCR_C_GREY}$(printf '─%.0s' {1..${#1}})${CCR_C_RESET}"
}

ccr_step_begin() {
  CCR_STEP_INDEX=$(( CCR_STEP_INDEX + 1 ))
  print -r -- "${CCR_C_BOLD}[${CCR_STEP_INDEX}/${CCR_STEP_TOTAL}]${CCR_C_RESET} $2 ${CCR_C_GREY}($1)${CCR_C_RESET}"
}

ccr_step_done_msg() { print -r -- "  ${CCR_C_GREEN}done${CCR_C_RESET}${1:+ ${CCR_C_GREY}$1${CCR_C_RESET}}"; }
ccr_step_skip_msg() { print -r -- "  ${CCR_C_GREY}already done${CCR_C_RESET}"; }
