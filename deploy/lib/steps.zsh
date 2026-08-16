# Step registration and runner. Resume is just deploy with the ledger consulted.

typeset -ga CCR_STEPS=()

ccr_step_register() { CCR_STEPS+=("$1"$'\t'"$2"$'\t'"$3"); }

ccr_steps_load() {
  local file
  CCR_STEPS=()
  for file in "${CCR_ROOT}"/steps/*.zsh(N); do source "$file"; done
  CCR_STEP_TOTAL=${#CCR_STEPS}
}

ccr_steps_run() {
  local entry id desc fn state rc
  for entry in "${CCR_STEPS[@]}"; do
    id="${entry%%$'\t'*}"
    desc="${${entry#*$'\t'}%%$'\t'*}"
    fn="${entry##*$'\t'}"
    state="$(ccr_step_status "$id")"
    ccr_step_begin "$id" "$desc"
    if [[ "$state" == "done" ]]; then ccr_step_skip_msg; continue; fi
    rc=0
    "$fn" || rc=$?
    if (( rc != 0 )); then
      ccr_step_record "$id" failed "" "exit ${rc}"
      ccr_blank; ccr_info "Stopped at ${id}. Run deploy/ccreport-azure resume when ready."
      return "$rc"
    fi
  done
}
