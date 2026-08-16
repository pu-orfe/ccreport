# Common preamble for ccreport's standalone Azure toolkit.

emulate -L zsh
setopt err_return no_unset pipe_fail

: "${CCREPORT_AZURE_LIB:?CCREPORT_AZURE_LIB must point at deploy/lib}"

typeset -g CCR_ROOT="${CCREPORT_AZURE_LIB:h}"
typeset -g CCR_DRY_RUN=0 CCR_ASSUME_YES=0 CCR_NO_REPROMPT=0 CCR_VERBOSE=0

for _ccr_lib in color log name state prompt az gh conf verify manual steps; do
  source "${CCREPORT_AZURE_LIB}/${_ccr_lib}.zsh"
done
unset _ccr_lib

ccr_color_init

ccr_parse_common_flags() {
  typeset -ga CCR_ARGS=()
  while (( $# )); do
    case "$1" in
      --dry-run) CCR_DRY_RUN=1 ;;
      --yes|-y|--non-interactive) CCR_ASSUME_YES=1 ;;
      --no-reprompt) CCR_NO_REPROMPT=1 ;;
      --verbose|-v) CCR_VERBOSE=1 ;;
      --app) shift; CCR_APP="$1" ;;
      --app=*) CCR_APP="${1#*=}" ;;
      --location) shift; CCR_LOCATION="$1" ;;
      --location=*) CCR_LOCATION="${1#*=}" ;;
      --prefix) shift; CCR_PREFIX="$1" ;;
      --prefix=*) CCR_PREFIX="${1#*=}" ;;
      --state) shift; CCR_STATE_FILE="$1" ;;
      --state=*) CCR_STATE_FILE="${1#*=}" ;;
      --until) shift; CCR_GATE_UNTIL="$1" ;;
      --until=*) CCR_GATE_UNTIL="${1#*=}" ;;
      --reason) shift; CCR_GATE_REASON="$1" ;;
      --reason=*) CCR_GATE_REASON="${1#*=}" ;;
      *) CCR_ARGS+=("$1") ;;
    esac
    shift
  done
}
