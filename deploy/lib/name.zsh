# Deterministic Azure names. The generated names are stored in the ledger so
# truncation and global uniqueness choices do not change between resumes.

typeset -g CCR_PREFIX="${CCR_PREFIX:-ccr}"
typeset -gA CCR_NAME_LIMITS=(rg 90 plan 40 webapp 60 acr 50 storage 24 db 63 identity 128 keyvault 24)
typeset -gA CCR_NAME_CHARSET=(rg dash plan dash webapp dash acr alnum storage alnum db dash identity dash keyvault dash)

ccr_slug() { print -r -- "${1:l}" | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'; }

ccr_name() {
  local kind="$1"; shift
  local limit="${CCR_NAME_LIMITS[$kind]:-60}" charset="${CCR_NAME_CHARSET[$kind]:-dash}"
  local joined="${CCR_PREFIX}-${(j:-:)@}"
  joined="$(ccr_slug "$joined")"
  [[ "$charset" == "alnum" ]] && joined="${joined//-/}"
  if (( ${#joined} > limit )); then
    local keep_tail=8 head_len=$(( limit - keep_tail ))
    joined="${joined[1,$head_len]}${joined[-$keep_tail,-1]}"
  fi
  print -r -- "$joined"
}

ccr_random_id() {
  local seed="$1"
  if command -v shasum >/dev/null 2>&1; then
    print -r -- "$(print -r -- "$seed" | shasum | cut -c1-6)"
  elif command -v sha256sum >/dev/null 2>&1; then
    print -r -- "$(print -r -- "$seed" | sha256sum | cut -c1-6)"
  else
    python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:6])' "$seed"
  fi
}
