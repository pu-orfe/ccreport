_ccr_step_registry() {
  ccr_az acr create --resource-group "$CCR_RG" --name "$CCR_ACR" --sku Basic --admin-enabled false -o none
  ccr_az acr update --name "$CCR_ACR" --admin-enabled false -o none
  ccr_state_set names.registry "$CCR_ACR"
  ccr_step_record registry done "${CCR_ACR}.azurecr.io"
  ccr_step_done_msg "$CCR_ACR"
}
ccr_step_register registry "Azure Container Registry" _ccr_step_registry
