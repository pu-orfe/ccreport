_ccr_step_verify() {
  local url="https://${CCR_WEBAPP}.azurewebsites.net" health="$(ccr_conf_get health_path /healthz)"
  if (( CCR_DRY_RUN )); then
    ccr_dim "dry-run: curl -fsS ${url}${health}"
    ccr_dim "dry-run: curl -I ${url}/  # expect 302, 401, or 403, never 200"
    ccr_dim "dry-run: curl -fsS ${url}/api/connectors/posture  # if implemented by the web app"
  else
    ccr_verify_app_responds || { ccr_err "Health endpoint did not return 200."; return 1; }
    ccr_verify_unauth_rejected || { ccr_err "Unauthenticated request was not rejected. Refusing to call this verified."; return 1; }
    curl -fsS --max-time 30 "${url}/api/connectors/posture" >/dev/null 2>&1 || ccr_warn "Connector posture endpoint was not available; confirm the web app exposes it."
  fi
  ccr_step_record verify done "$url"
  ccr_step_done_msg "$url"
}
ccr_step_register verify "Health, auth rejection and connector posture" _ccr_step_verify
