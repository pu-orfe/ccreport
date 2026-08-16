_ccr_step_image() {
  [[ -f Dockerfile ]] || { ccr_warn "No Dockerfile found."; return 1; }
  ccr_az acr build --registry "$CCR_ACR" --image "${CCR_IMAGE}:latest" --image "${CCR_IMAGE}:${CCR_IMAGE_TAG}" --build-arg INSTALL_PLAYWRIGHT=true --file Dockerfile . -o none
  ccr_state_set names.image "${CCR_ACR}.azurecr.io/${CCR_IMAGE}:${CCR_IMAGE_TAG}"
  ccr_step_record image done "${CCR_ACR}.azurecr.io/${CCR_IMAGE}:${CCR_IMAGE_TAG}"
  ccr_step_done_msg "${CCR_IMAGE}:${CCR_IMAGE_TAG}"
}
ccr_step_register image "Build and push image with az acr build" _ccr_step_image
