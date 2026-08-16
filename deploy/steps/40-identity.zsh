_ccr_step_identity() {
  ccr_az identity create --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --location "$CCR_LOCATION" -o none
  local principal client acr_id
  principal="$(ccr_az_query identity show --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --query principalId -o tsv)"; principal="${principal:-<principal-id>}"
  client="$(ccr_az_query identity show --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --query clientId -o tsv)"; client="${client:-<client-id>}"
  ccr_az webapp identity assign --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --identities "$CCR_IDENTITY" -o none
  ccr_az webapp config set --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --acr-use-identity --acr-identity "$client" -o none
  acr_id="$(ccr_az_query acr show --name "$CCR_ACR" --query id -o tsv)"; acr_id="${acr_id:-/subscriptions/<subscription>/resourceGroups/${CCR_RG}/providers/Microsoft.ContainerRegistry/registries/${CCR_ACR}}"
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$acr_id" -o none
  ccr_state_set names.identity "$CCR_IDENTITY"
  ccr_state_set azure.identityClientId "$client"
  ccr_step_record identity done "$client"
  ccr_step_done_msg "$CCR_IDENTITY"
}
ccr_step_register identity "User-assigned managed identity and AcrPull" _ccr_step_identity
