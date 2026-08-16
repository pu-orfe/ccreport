_ccr_step_oidc() {
  local repo client principal rg_scope acr_id webapp_id subject cred
  repo="$(ccr_gh_repo)"
  client="$(ccr_state_get azure.identityClientId "<client-id>")"
  principal="$(ccr_az_query identity show --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --query principalId -o tsv)"; principal="${principal:-<principal-id>}"
  rg_scope="/subscriptions/${CCR_SUBSCRIPTION:-<subscription>}/resourceGroups/${CCR_RG}"
  acr_id="$(ccr_az_query acr show --name "$CCR_ACR" --query id -o tsv)"; acr_id="${acr_id:-${rg_scope}/providers/Microsoft.ContainerRegistry/registries/${CCR_ACR}}"
  webapp_id="$(ccr_az_query webapp show --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --query id -o tsv)"; webapp_id="${webapp_id:-${rg_scope}/providers/Microsoft.Web/sites/${CCR_WEBAPP}}"
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role AcrPush --scope "$acr_id" -o none
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role "Website Contributor" --scope "$webapp_id" -o none
  for subject in "repo:${repo}:ref:refs/heads/main" "repo:${repo}:environment:production"; do
    cred="gh-$(print -r -- "${subject##*:}" | tr '/:' '--')"
    ccr_az identity federated-credential create --resource-group "$CCR_RG" --identity-name "$CCR_IDENTITY" --name "$cred" --issuer "https://token.actions.githubusercontent.com" --subject "$subject" --audiences "api://AzureADTokenExchange" -o none
  done
  ccr_gh_var_set "$repo" AZURE_CLIENT_ID "$client"
  ccr_gh_var_set "$repo" AZURE_TENANT_ID "${CCR_TENANT:-<tenant-id>}"
  ccr_gh_var_set "$repo" AZURE_SUBSCRIPTION_ID "${CCR_SUBSCRIPTION:-<subscription-id>}"
  ccr_gh_var_set "$repo" AZURE_RESOURCE_GROUP "$CCR_RG"
  ccr_gh_var_set "$repo" AZURE_WEBAPP_NAME "$CCR_WEBAPP"
  ccr_gh_var_set "$repo" ACR_NAME "$CCR_ACR"
  ccr_step_record oidc done "$client"
  ccr_step_done_msg "${repo}"
}
ccr_step_register oidc "GitHub OIDC and repository variables" _ccr_step_oidc
