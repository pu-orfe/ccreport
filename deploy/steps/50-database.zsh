_ccr_step_database() {
  local db_user="ccreportadmin" db_pass
  db_pass="$(ccr_state_get azure.databasePassword "")"
  if [[ -z "$db_pass" ]]; then
    if (( CCR_DRY_RUN )); then db_pass="<generated-password>"; else db_pass="$(python3 -c 'import secrets,string; alphabet=string.ascii_letters+string.digits; print("Cc1!"+"".join(secrets.choice(alphabet) for _ in range(28)))')"; fi
    ccr_state_set azure.databasePassword "<stored-in-key-vault-not-ledger>"
  fi
  ccr_az postgres flexible-server create --resource-group "$CCR_RG" --name "$CCR_DB_SERVER" --location "$CCR_LOCATION" --admin-user "$db_user" --admin-password "$db_pass" --sku-name "$(ccr_conf_get postgres_sku Standard_B1ms)" --tier Burstable --storage-size "$(ccr_conf_get postgres_storage_gb 32)" --version 16 --yes -o none
  ccr_az postgres flexible-server db create --resource-group "$CCR_RG" --server-name "$CCR_DB_SERVER" --database-name ccreport -o none
  ccr_az postgres flexible-server firewall-rule create --resource-group "$CCR_RG" --name "$CCR_DB_SERVER" --rule-name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none
  ccr_state_set names.database "$CCR_DB_SERVER"
  ccr_state_set azure.databaseUser "$db_user"
  ccr_step_record database done "$CCR_DB_SERVER.postgres.database.azure.com"
  ccr_step_done_msg "$CCR_DB_SERVER"
}
ccr_step_register database "PostgreSQL Flexible Server B1ms" _ccr_step_database
