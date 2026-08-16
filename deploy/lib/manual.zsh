# Manual gates are explicit pauses for Princeton OIT work, not silent TODOs.

ccr_await_manual_step() {
  local id="" title="" verify="" portal="" checklist="" risk="normal"
  while (( $# )); do
    case "$1" in
      --id) shift; id="$1" ;;
      --title) shift; title="$1" ;;
      --verify) shift; verify="$1" ;;
      --portal) shift; portal="$1" ;;
      --checklist) shift; checklist="$1" ;;
      --risk) shift; risk="$1" ;;
      *) ccr_die "ccr_await_manual_step: unknown option $1" ;;
    esac
    shift
  done

  if [[ -n "$verify" ]] && "$verify" 2>/dev/null; then
    ccr_step_record "$id" done "" "already configured"
    ccr_step_done_msg "already configured"
    return 0
  fi
  if ccr_gate_acked "$id"; then
    ccr_warn "$title — skipped by an acknowledgement that has not expired."
    return 0
  fi

  ccr_blank
  print -r -- "  ${CCR_C_YELLOW}${CCR_C_BOLD}Manual gate:${CCR_C_RESET} ${title}"
  local -a items
  items=(${(s:|:)checklist})
  local i=1 item
  for item in $items; do
    print -r -- "    ${CCR_C_BOLD}${i}.${CCR_C_RESET} ${item}"
    i=$(( i + 1 ))
  done
  [[ -n "$portal" ]] && print -r -- "    ${CCR_C_BLUE}${portal}${CCR_C_RESET}"

  if (( CCR_DRY_RUN )); then
    ccr_dim "  dry-run: would wait here for ${id}; acknowledging no real gate."
    return 0
  fi
  if (( CCR_ASSUME_YES )); then
    ccr_err "$title is not complete and this run is non-interactive."
    ccr_dim "Finish the checklist, then: deploy/ccreport-azure gate ack ${id} --until YYYY-MM-DD --reason '<ticket>'"
    ccr_step_record "$id" pending "" "blocked: manual gate"
    return 1
  fi

  ccr_confirm "Have these steps been completed?" n || { ccr_step_record "$id" pending "" "operator paused"; return 1; }
  if [[ -n "$verify" ]] && ! "$verify"; then
    ccr_warn "Verification did not pass. Refusing to mark a critical gate done."
    ccr_step_record "$id" failed "" "verification failed"
    return 1
  fi
  ccr_step_record "$id" done
  ccr_step_done_msg
}
