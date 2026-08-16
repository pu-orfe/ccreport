_ccr_step_keyvault() {
  local principal kv_id key_name db_user db_url
  key_name="$(ccr_conf_get keyvault_key_name ccreport-token-dek)"
  ccr_az keyvault create --resource-group "$CCR_RG" --name "$CCR_KEYVAULT" --location "$CCR_LOCATION" --enable-rbac-authorization true --sku standard -o none
  ccr_az keyvault key create --vault-name "$CCR_KEYVAULT" --name "$key_name" --kty RSA --size 2048 --ops wrapKey unwrapKey -o none
  principal="$(ccr_az_query identity show --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --query principalId -o tsv)"; principal="${principal:-<principal-id>}"
  kv_id="$(ccr_az_query keyvault show --name "$CCR_KEYVAULT" --query id -o tsv)"; kv_id="${kv_id:-/subscriptions/<subscription>/resourceGroups/${CCR_RG}/providers/Microsoft.KeyVault/vaults/${CCR_KEYVAULT}}"
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role "Key Vault Crypto User" --scope "$kv_id" -o none
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope "$kv_id" -o none
  db_user="$(ccr_state_get azure.databaseUser ccreportadmin)"
  db_url="postgresql+psycopg://${db_user}:<db-password>@${CCR_DB_SERVER}.postgres.database.azure.com:5432/ccreport?sslmode=require"
  ccr_az keyvault secret set --vault-name "$CCR_KEYVAULT" --name ccreport-database-url --value "$db_url" -o none
  ccr_az keyvault secret set --vault-name "$CCR_KEYVAULT" --name ccreport-session-secret --value "<generated-session-secret>" -o none
  ccr_state_set names.keyVault "$CCR_KEYVAULT"
  ccr_state_set azure.keyName "$key_name"
  ccr_step_record keyvault done "https://${CCR_KEYVAULT}.vault.azure.net/"
  ccr_step_done_msg "$CCR_KEYVAULT"
}
ccr_step_register keyvault "Key Vault, wrapping key and secrets" _ccr_step_keyvault
