_ccr_step_resource_group() {
  ccr_az group create --name "$CCR_RG" --location "$CCR_LOCATION" -o none
  ccr_step_record resource-group done "/subscriptions/${CCR_SUBSCRIPTION:-<subscription>}/resourceGroups/${CCR_RG}"
  ccr_step_done_msg "$CCR_RG"
}
ccr_step_register resource-group "Resource group" _ccr_step_resource_group
