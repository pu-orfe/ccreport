# The resumable, committed ledger. It records names and step status, never secrets.

typeset -g CCR_STATE_FILE="${CCR_STATE_FILE:-.ccreport/state.json}"

_ccr_py() { python3 -c "$1" "${@:2}"; }

ccr_state_init() {
  local app="$1"
  [[ -f "$CCR_STATE_FILE" ]] && return 0
  mkdir -p "${CCR_STATE_FILE:h}"
  _ccr_py '
import json, sys
path, app = sys.argv[1], sys.argv[2]
doc = {"schemaVersion": 1, "app": app, "azure": {}, "names": {}, "prompts": {}, "steps": [], "history": []}
with open(path, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
' "$CCR_STATE_FILE" "$app"
  ccr_dim "created ${CCR_STATE_FILE}"
}

_ccr_state_py='
def _load(path):
    import json
    with open(path) as fh:
        return json.load(fh)
def _save(path, doc):
    import json
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
'

ccr_state_get() {
  [[ -f "$CCR_STATE_FILE" ]] || { print -r -- "${2:-}"; return 0; }
  _ccr_py "${_ccr_state_py}"'
import sys
path, dotted, default = sys.argv[1:4]
node = _load(path)
for part in [p for p in dotted.split(".") if p]:
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        print(default); raise SystemExit
print("" if node is None else node)
' "$CCR_STATE_FILE" "$1" "${2:-}"
}

ccr_state_set() {
  _ccr_py "${_ccr_state_py}"'
import sys
path, dotted, value = sys.argv[1:4]
doc = _load(path)
node = doc
parts = [p for p in dotted.split(".") if p]
for part in parts[:-1]:
    node = node.setdefault(part, {})
node[parts[-1]] = value
_save(path, doc)
' "$CCR_STATE_FILE" "$1" "$2"
}

ccr_step_status() {
  [[ -f "$CCR_STATE_FILE" ]] || { print -r -- pending; return 0; }
  _ccr_py "${_ccr_state_py}"'
import sys
path, sid = sys.argv[1:3]
doc = _load(path)
for step in doc.get("steps", []):
    if step.get("id") == sid:
        print(step.get("status", "pending")); raise SystemExit
print("pending")
' "$CCR_STATE_FILE" "$1"
}

ccr_step_record() {
  _ccr_py "${_ccr_state_py}"'
import datetime, sys
path, sid, status, resource, note = sys.argv[1:6]
doc = _load(path)
entry = next((s for s in doc.setdefault("steps", []) if s.get("id") == sid), None)
if entry is None:
    entry = {"id": sid}; doc["steps"].append(entry)
entry["status"] = status
entry["at"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
if resource: entry["resourceId"] = resource
if note: entry["note"] = note
_save(path, doc)
' "$CCR_STATE_FILE" "$1" "$2" "${3:-}" "${4:-}"
}

ccr_gate_ack() {
  _ccr_py "${_ccr_state_py}"'
import sys
path, sid, until, reason = sys.argv[1:5]
doc = _load(path)
entry = next((s for s in doc.setdefault("steps", []) if s.get("id") == sid), None)
if entry is None:
    entry = {"id": sid, "status": "skipped"}; doc["steps"].append(entry)
entry["ackUntil"] = until
entry["reason"] = reason
_save(path, doc)
' "$CCR_STATE_FILE" "$1" "$2" "$3"
}

ccr_gate_acked() {
  [[ -f "$CCR_STATE_FILE" ]] || return 1
  _ccr_py "${_ccr_state_py}"'
import datetime, sys
path, sid = sys.argv[1:3]
doc = _load(path)
for step in doc.get("steps", []):
    if step.get("id") == sid and step.get("ackUntil"):
        raise SystemExit(0 if datetime.date.fromisoformat(step["ackUntil"]) >= datetime.date.today() else 1)
raise SystemExit(1)
' "$CCR_STATE_FILE" "$1"
}

ccr_prompt_record() { ccr_state_set "prompts.$1" "$2"; }

ccr_state_history() {
  local verb="$1" azv
  azv="$(ccr_az_version 2>/dev/null || print -r -- unknown)"
  _ccr_py "${_ccr_state_py}"'
import datetime, sys
path, verb, azv, version = sys.argv[1:5]
doc = _load(path)
doc.setdefault("history", []).append({"at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "verb": verb, "azVersion": azv, "ccreportAzure": version})
doc["history"] = doc["history"][-50:]
_save(path, doc)
' "$CCR_STATE_FILE" "$verb" "$azv" "${CCR_VERSION:-unknown}"
}

ccr_steps_report() {
  _ccr_py '
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("  no ledger yet"); raise SystemExit
mark = {"done":"✓", "pending":"·", "failed":"x", "skipped":"s"}
for step in doc.get("steps", []):
    extra = ""
    if step.get("ackUntil"): extra = " (acknowledged until %s)" % step["ackUntil"]
    elif step.get("note"): extra = " (%s)" % step["note"]
    print("  %s %s %s%s" % (mark.get(step.get("status"), "?"), step.get("id", "").ljust(24), step.get("status", "pending"), extra))
' "$CCR_STATE_FILE"
}
