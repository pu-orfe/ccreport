_ccr_step_storage() {
  local container="$(ccr_conf_get blob_container ccreport-artifacts)" principal storage_id
  ccr_az storage account create --resource-group "$CCR_RG" --name "$CCR_STORAGE" --location "$CCR_LOCATION" --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false -o none
  ccr_az storage container create --account-name "$CCR_STORAGE" --name "$container" --auth-mode login --public-access off -o none
  principal="$(ccr_az_query identity show --resource-group "$CCR_RG" --name "$CCR_IDENTITY" --query principalId -o tsv)"; principal="${principal:-<principal-id>}"
  storage_id="$(ccr_az_query storage account show --resource-group "$CCR_RG" --name "$CCR_STORAGE" --query id -o tsv)"; storage_id="${storage_id:-/subscriptions/<subscription>/resourceGroups/${CCR_RG}/providers/Microsoft.Storage/storageAccounts/${CCR_STORAGE}}"
  ccr_az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" --scope "$storage_id" -o none
  ccr_state_set names.storage "$CCR_STORAGE"
  ccr_state_set azure.blobContainer "$container"
  ccr_step_record storage done "https://${CCR_STORAGE}.blob.core.windows.net/${container}"
  ccr_step_done_msg "$CCR_STORAGE/$container"
}
ccr_step_register storage "Blob storage for report artifacts" _ccr_step_storage
