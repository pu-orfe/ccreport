_ccr_step_scale_guard() {
  local health="$(ccr_conf_get health_path /healthz)"
  ccr_az webapp update --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --https-only true -o none
  ccr_az webapp config set --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --always-on true --min-tls-version 1.2 --number-of-workers 1 --health-check-path "$health" -o none
  ccr_az resource update --resource-group "$CCR_RG" --resource-type Microsoft.Web/sites/config --name "${CCR_WEBAPP}/web" --set properties.minimumElasticInstanceCount=0 -o none
  ccr_step_record scale-guard done "" "single-instance until verified safe"
  ccr_step_done_msg "Always On, health check, HTTPS, TLS 1.2, one worker"
}
ccr_step_register scale-guard "Runtime safety guardrails" _ccr_step_scale_guard
