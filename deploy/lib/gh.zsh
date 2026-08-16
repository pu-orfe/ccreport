# GitHub helpers for OIDC setup and repository configuration.

ccr_gh_repo() {
  local remote
  remote="$(git remote get-url origin 2>/dev/null)" || { print -r -- "pu-orfe/ccreport"; return 0; }
  print -r -- "$remote" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##'
}

ccr_gh_var_set() {
  local repo="$1" key="$2" value="$3"
  (( CCR_DRY_RUN )) && { print -r -- "  ${CCR_C_GREY}dry-run:${CCR_C_RESET} gh variable set ${key} --repo ${repo}"; return 0; }
  gh variable set "$key" --repo "$repo" --body "$value" >/dev/null
}

ccr_gh_secret_set() {
  local repo="$1" key="$2" value="$3"
  (( CCR_DRY_RUN )) && { print -r -- "  ${CCR_C_GREY}dry-run:${CCR_C_RESET} gh secret set ${key} --repo ${repo}"; return 0; }
  gh secret set "$key" --repo "$repo" --body "$value" >/dev/null
}
