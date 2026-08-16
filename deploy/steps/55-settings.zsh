_ccr_setting_pairs=()

_ccr_kv_ref() {
  local secret="$1"
  print -r -- "@Microsoft.KeyVault(SecretUri=https://${CCR_KEYVAULT}.vault.azure.net/secrets/${secret}/)"
}

_ccr_collect_setting() {
  local name="$1" kind="$2" required="$3" value="$4" generate="$5" prompt="$6" kv_secret="$7"
  local setting_value=""
  case "$kind" in
    fixed|bool)
      setting_value="$value" ;;
    computed)
      case "$name" in
        CCREPORT_BASE_URL) setting_value="https://${CCR_WEBAPP}.azurewebsites.net" ;;
        CCREPORT_BLOB_ACCOUNT_URL) setting_value="https://${CCR_STORAGE}.blob.core.windows.net" ;;
        CCREPORT_BLOB_CONTAINER) setting_value="$(ccr_conf_get blob_container ccreport-artifacts)" ;;
        CCREPORT_KEYVAULT_URL) setting_value="https://${CCR_KEYVAULT}.vault.azure.net/" ;;
        CCREPORT_KEYVAULT_KEY_NAME) setting_value="$(ccr_conf_get keyvault_key_name ccreport-token-dek)" ;;
      esac ;;
    secret)
      if [[ -n "$kv_secret" ]]; then
        setting_value="$(_ccr_kv_ref "$kv_secret")"
      elif (( CCR_DRY_RUN )); then
        setting_value="<dry-run-secret>"
      else
        ccr_ask setting_value "${prompt:-Value for ${name}}"
      fi ;;
    list)
      setting_value="$(ccr_state_get "prompts.${name}" "")"
      if [[ -z "$setting_value" ]]; then
        if (( CCR_DRY_RUN )); then
          setting_value=""
          ccr_dim "  ${name}=<empty> (deny-all until configured)"
        elif [[ "$required" == "True" || "$required" == "true" ]]; then
          ccr_ask setting_value "${prompt:-Value for ${name}}" "" ccr_valid_emails "$name"
        fi
      fi ;;
    *)
      setting_value="$value" ;;
  esac
  _ccr_setting_pairs+=("${name}=${setting_value}")
}

_ccr_step_settings() {
  _ccr_setting_pairs=()
  ccr_conf_each_setting _ccr_collect_setting
  _ccr_setting_pairs+=("WEBSITES_PORT=8000")
  _ccr_setting_pairs+=("WEBSITES_CONTAINER_START_TIME_LIMIT=600")
  ccr_az webapp config appsettings set --resource-group "$CCR_RG" --name "$CCR_WEBAPP" --settings "${_ccr_setting_pairs[@]}" -o none
  ccr_step_record settings done "" "${#_ccr_setting_pairs} app settings"
  ccr_step_done_msg "${#_ccr_setting_pairs} settings"
}
ccr_step_register settings "Application settings from deploy/app.conf" _ccr_step_settings
