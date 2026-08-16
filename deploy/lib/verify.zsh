# Read-only predicates used by doctor, drift and manual gates.

ccr_verify_easy_auth() {
  (( CCR_DRY_RUN )) && return 1
  local enabled unauth
  enabled="$(ccr_az_query webapp auth show --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --query "platform.enabled" -o tsv)"
  unauth="$(ccr_az_query webapp auth show --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --query "globalValidation.unauthenticatedClientAction" -o tsv)"
  [[ "${enabled:l}" == "true" && "$unauth" != "AllowAnonymous" ]]
}

ccr_verify_app_responds() {
  command -v curl >/dev/null 2>&1 || return 1
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://${CCR_WEBAPP}.azurewebsites.net${CCR_HEALTH_PATH:-/healthz}" 2>/dev/null)"
  [[ "$code" == "200" ]]
}

ccr_verify_unauth_rejected() {
  command -v curl >/dev/null 2>&1 || return 1
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://${CCR_WEBAPP}.azurewebsites.net/" 2>/dev/null)"
  [[ "$code" == 30* || "$code" == "401" || "$code" == "403" ]]
}

ccr_verify_gh_auth() { command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; }
