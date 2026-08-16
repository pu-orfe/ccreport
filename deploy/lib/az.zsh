# All Azure calls pass through here so dry-run and verbose behavior are uniform.

typeset -g CCR_AZ_MIN="2.60.0"

ccr_semver_ge() {
  local -a a b
  a=(${(s:.:)1}) b=(${(s:.:)2})
  local i x y
  for i in 1 2 3; do
    x="${a[$i]:-0}"; y="${b[$i]:-0}"
    x="${x%%[^0-9]*}"; y="${y%%[^0-9]*}"
    (( ${x:-0} > ${y:-0} )) && return 0
    (( ${x:-0} < ${y:-0} )) && return 1
  done
  return 0
}

ccr_az_version() { (( CCR_DRY_RUN )) && { print -r -- dry-run; return 0; }; az version --query '"azure-cli"' -o tsv 2>/dev/null; }

ccr_az_version_check() {
  (( CCR_DRY_RUN )) && { ccr_dim "az version check skipped in dry-run"; return 0; }
  local version
  version="$(ccr_az_version)" || { ccr_warn "Could not determine az version."; return 0; }
  if ! ccr_semver_ge "$version" "$CCR_AZ_MIN"; then ccr_die "az $version is older than $CCR_AZ_MIN."; fi
  ccr_dim "az $version"
}

ccr_az() {
  if (( CCR_DRY_RUN )); then
    print -r -- "  ${CCR_C_GREY}dry-run:${CCR_C_RESET} az ${(j: :)@}"
    return 0
  fi
  (( CCR_VERBOSE )) && ccr_dim "az ${(j: :)@}"
  local out rc
  out="$(az "$@" 2>&1)" || { rc=$?; ccr_err "az ${(j: :)@}"; print -r -- "$out" >&2; return $rc; }
  [[ -n "$out" ]] && print -r -- "$out"
}

ccr_az_query() {
  (( CCR_DRY_RUN )) && { print -r -- ""; return 0; }
  az "$@" 2>/dev/null || true
}

ccr_az_exists() {
  (( CCR_DRY_RUN )) && return 1
  local result
  result="$(ccr_az_query "$@")"
  [[ -n "$result" && "$result" != "null" && "$result" != "[]" ]]
}

ccr_require_login() {
  (( CCR_DRY_RUN )) && { ccr_dim "dry-run: Azure sign-in not required"; return 0; }
  az account show >/dev/null 2>&1 || ccr_die "Not signed in to Azure. Run: az login"
}
