# app.conf is the small declarative contract; scripts should not hard-code settings.

typeset -g CCR_CONF_FILE=""
typeset -gA CCR_CONF=()
typeset -ga CCR_CONF_SETTINGS=()

ccr_conf_load() {
  local file="$1"
  [[ -f "$file" ]] || ccr_die "No app.conf at ${file}."
  CCR_CONF_FILE="$file"
  CCR_CONF=(); CCR_CONF_SETTINGS=()
  local dumped
  dumped="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    doc = tomllib.load(fh)
for key, value in doc.items():
    if key == "setting":
        continue
    if isinstance(value, bool):
        value = "true" if value else "false"
    if not isinstance(value, (dict, list)):
        print(f"SCALAR\t{key}\t{value}")
for setting in doc.get("setting", []) or []:
    print("SETTING\t" + "\t".join(str(setting.get(k, "")) for k in ("name","type","required","value","generate","prompt","keyvault_secret")))
' "$file")" || ccr_die "Could not parse ${file}."
  local line kind rest
  while IFS= read -r line; do
    kind="${line%%$'\t'*}"; rest="${line#*$'\t'}"
    case "$kind" in
      SCALAR) CCR_CONF[${rest%%$'\t'*}]="${rest#*$'\t'}" ;;
      SETTING) CCR_CONF_SETTINGS+=("$rest") ;;
    esac
  done <<< "$dumped"
  [[ -n "${CCR_CONF[name]:-}" ]] || ccr_die "${file} declares no name."
}

ccr_conf_get() { print -r -- "${CCR_CONF[$1]:-${2:-}}"; }

ccr_conf_each_setting() {
  local handler="$1" entry
  for entry in "${CCR_CONF_SETTINGS[@]}"; do
    local -a f
    f=("${(@s:	:)entry}")
    while (( ${#f} < 7 )); do f+=(""); done
    "$handler" "${f[1]}" "${f[2]}" "${f[3]}" "${f[4]}" "${f[5]}" "${f[6]}" "${f[7]}"
  done
}
