# Prompts record non-secret answers in the ledger so --no-reprompt can replay them.

ccr_ask() {
  local var="$1" question="$2" default="${3:-}" validator="${4:-}" key="${5:-$1}"
  local current="${(P)var:-}" recorded
  [[ -n "$current" ]] && default="$current"
  recorded="$(ccr_state_get "prompts.${key}" "")"
  [[ -n "$recorded" ]] && default="$recorded"

  if (( CCR_ASSUME_YES || CCR_NO_REPROMPT )); then
    [[ -z "$default" ]] && (( ! CCR_DRY_RUN )) && ccr_die "$question — no value, and running non-interactively."
    [[ -z "$default" ]] && default="<dry-run>"
    typeset -g "$var"="$default"
    ccr_prompt_record "$key" "$default"
    return 0
  fi

  local answer
  while true; do
    if [[ -n "$default" ]]; then
      print -n -- "${CCR_C_BOLD}?${CCR_C_RESET} ${question} ${CCR_C_GREY}[${default}]${CCR_C_RESET} "
    else
      print -n -- "${CCR_C_BOLD}?${CCR_C_RESET} ${question} "
    fi
    read -r answer || answer=""
    [[ -z "$answer" ]] && answer="$default"
    if [[ -z "$answer" ]]; then ccr_warn "A value is required."; continue; fi
    if [[ -n "$validator" ]] && ! "$validator" "$answer"; then continue; fi
    typeset -g "$var"="$answer"
    ccr_prompt_record "$key" "$answer"
    return 0
  done
}

ccr_confirm() {
  local question="$1" default="${2:-n}"
  (( CCR_ASSUME_YES )) && return 0
  local hint="[y/N]" answer
  [[ "$default" == "y" ]] && hint="[Y/n]"
  print -n -- "${CCR_C_BOLD}?${CCR_C_RESET} ${question} ${CCR_C_GREY}${hint}${CCR_C_RESET} "
  read -r answer || answer=""
  [[ -z "$answer" ]] && answer="$default"
  [[ "${answer:l}" == y* ]]
}

ccr_valid_emails() {
  local entry
  for entry in ${(s:,:)1}; do
    entry="${entry## }"; entry="${entry%% }"
    [[ -z "$entry" ]] && continue
    if [[ ! "$entry" =~ '^(@[^@[:space:]]+\.[^@[:space:]]+|[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+)$' ]]; then
      ccr_warn "Not an address or @domain rule: $entry"
      return 1
    fi
  done
  return 0
}

ccr_valid_name() { [[ "$1" =~ '^[a-zA-Z0-9][a-zA-Z0-9-]{1,58}[a-zA-Z0-9]$' ]] || { ccr_warn "Use 2-60 letters, digits or dashes; no leading/trailing dash."; return 1; }; }
ccr_valid_location() { [[ "$1" =~ '^[a-z][a-z0-9]+$' ]] || { ccr_warn "Use an Azure region like eastus."; return 1; }; }
