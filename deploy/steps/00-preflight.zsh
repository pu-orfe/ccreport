_ccr_step_preflight() {
  local ok=1 tool
  for tool in python3 az gh git docker; do
    if command -v "$tool" >/dev/null 2>&1; then ccr_ok "$tool found"; else ccr_warn "$tool missing"; ok=0; fi
  done
  ccr_az_version_check
  if (( ! CCR_DRY_RUN )); then
    az account show >/dev/null 2>&1 || { ccr_warn "Azure is not signed in; run az login."; ok=0; }
    gh auth status >/dev/null 2>&1 || { ccr_warn "GitHub CLI is not signed in; run gh auth login."; ok=0; }
  else
    ccr_dim "dry-run: no Azure or GitHub login checked"
  fi
  (( ok || CCR_DRY_RUN )) || return 1
  ccr_state_set azure.location "$CCR_LOCATION"
  ccr_step_record preflight done
  ccr_step_done_msg
}
ccr_step_register preflight "Preflight checks" _ccr_step_preflight
