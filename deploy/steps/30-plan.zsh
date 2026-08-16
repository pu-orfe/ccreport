_ccr_step_plan() {
  ccr_az appservice plan create --resource-group "$CCR_RG" --name "$CCR_PLAN" --location "$CCR_LOCATION" --is-linux --sku "$(ccr_conf_get app_service_plan_sku B1)" -o none
  ccr_state_set names.plan "$CCR_PLAN"
  ccr_step_record plan done "$CCR_PLAN"
  ccr_step_done_msg "$CCR_PLAN"
}
ccr_step_register plan "Linux App Service plan" _ccr_step_plan
