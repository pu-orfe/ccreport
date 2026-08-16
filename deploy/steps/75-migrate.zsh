_ccr_step_migrate() {
  local image="${CCR_ACR}.azurecr.io/${CCR_IMAGE}:${CCR_IMAGE_TAG}"
  ccr_az webapp ssh --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --command "ccreport db upgrade" || ccr_dim "If SSH is unavailable, run the same command through the App Service console using image ${image}."
  ccr_step_record migrate done "" "ccreport db upgrade"
  ccr_step_done_msg "ccreport db upgrade"
}
ccr_step_register migrate "Run Alembic migrations inside the deployed container" _ccr_step_migrate
