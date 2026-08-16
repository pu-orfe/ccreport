_ccr_step_webapp() {
  local image="${CCR_ACR}.azurecr.io/${CCR_IMAGE}:${CCR_IMAGE_TAG}"
  ccr_az webapp create --resource-group "$CCR_RG" --plan "$CCR_PLAN" --name "$CCR_WEBAPP" --deployment-container-image-name "$image" -o none
  ccr_az webapp config container set --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --container-image-name "$image" -o none
  ccr_state_set names.webApp "$CCR_WEBAPP"
  ccr_step_record webapp done "https://${CCR_WEBAPP}.azurewebsites.net"
  ccr_step_done_msg "https://${CCR_WEBAPP}.azurewebsites.net"
}
ccr_step_register webapp "Web app container host" _ccr_step_webapp
