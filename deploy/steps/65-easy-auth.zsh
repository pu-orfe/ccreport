_ccr_step_easy_auth() {
  local tenant portal
  tenant="${CCR_TENANT:-<tenant-id>}"
  portal="https://portal.azure.com/#@${tenant}/resource/subscriptions/${CCR_SUBSCRIPTION:-<subscription>}/resourceGroups/${CCR_RG}/providers/Microsoft.Web/sites/${CCR_WEBAPP}/authentication"
  ccr_blank
  ccr_dim "Suggested az commands after OIT creates the app registration:"
  ccr_dim "  az webapp auth update --resource-group ${CCR_RG} --name ${CCR_WEBAPP} --enabled true --action LoginWithAzureActiveDirectory"
  ccr_dim "  az webapp auth microsoft update --resource-group ${CCR_RG} --name ${CCR_WEBAPP} --client-id <entra-app-client-id> --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --issuer https://sts.windows.net/${tenant}/"
  ccr_await_manual_step \
    --id easy-auth \
    --risk critical \
    --title "Entra ID Easy Auth must reject anonymous traffic for ${CCR_WEBAPP}" \
    --verify ccr_verify_easy_auth \
    --portal "$portal" \
    --checklist "Request an OIT-owned single-tenant Entra app registration for ${CCR_WEBAPP}.|Redirect URI: https://${CCR_WEBAPP}.azurewebsites.net/.auth/login/aad/callback|Configure App Service Authentication with Microsoft as the identity provider.|Set unauthenticated requests to require authentication, never allow anonymous.|Run the az webapp auth commands above or make the equivalent portal changes.|Verify an unauthenticated browser request is redirected or rejected."
  ccr_step_record easy-auth done
}
ccr_step_register easy-auth "Easy Auth manual gate and enforcement check" _ccr_step_easy_auth
