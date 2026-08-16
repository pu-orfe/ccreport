# Colour and spinner support, respecting pipes, NO_COLOR and very small CI terminals.

ccr_color_init() {
  if [[ -n "${NO_COLOR:-}" || ! -t 1 || "${TERM:-dumb}" == "dumb" ]]; then
    CCR_C_RESET="" CCR_C_BOLD="" CCR_C_DIM=""
    CCR_C_RED="" CCR_C_GREEN="" CCR_C_YELLOW="" CCR_C_BLUE="" CCR_C_CYAN="" CCR_C_GREY=""
    CCR_COLOR=0
  else
    CCR_C_RESET=$'\e[0m'  CCR_C_BOLD=$'\e[1m'   CCR_C_DIM=$'\e[2m'
    CCR_C_RED=$'\e[31m'   CCR_C_GREEN=$'\e[32m' CCR_C_YELLOW=$'\e[33m'
    CCR_C_BLUE=$'\e[34m'  CCR_C_CYAN=$'\e[36m'  CCR_C_GREY=$'\e[90m'
    CCR_COLOR=1
  fi
}

ccr_spinner_frames() {
  if [[ "${CCR_COLOR:-0}" == "1" && "${LANG:-}" == *[Uu][Tt][Ff]* ]]; then
    print -r -- "⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏"
  else
    print -r -- "| / - \\"
  fi
}
