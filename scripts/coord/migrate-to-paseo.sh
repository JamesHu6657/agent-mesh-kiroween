#!/usr/bin/env bash
# One-shot migration of the Kiro coordinator from the tmux runner to a native Paseo (kiro ACP) agent,
# at a safe boundary: waits until the tmux runner has finished its current turn and exited (STOP file),
# then imports the SAME Kiro session into Paseo and continues it there. Never runs two coordinators.
cd "$(dirname "$0")/../.."
C=docs/coordination; L=logs/raw/paseo-migrate.log; SID_FALLBACK="${MESH_PASEO_SESSION:-}"
log(){ echo "$(TZ=UTC date '+%F %T') $*" | tee -a "$L"; }
log "waiting for tmux runner kiro-coord to exit at turn boundary"
while tmux has-session -t kiro-coord 2>/dev/null || pgrep -f 'kiro-cli chat --no-interactive --agent mesh-coordinator' >/dev/null; do sleep 15; done
SID=$(cat "$C/.session" 2>/dev/null || echo "$SID_FALLBACK")
[ -n "$SID" ] || { log "no session id (docs/coordination/.session or MESH_PASEO_SESSION)"; exit 1; }
log "runner exited; session=$SID"
AID=$(paseo import "$SID" --provider kiro --cwd "$PWD" --label role=mesh-coordinator --json 2>>"$L" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("agentId",""))' 2>>"$L")
if [ -z "$AID" ]; then log "IMPORT FAILED -> restarting tmux runner (fallback)"; rm -f "$C/STOP"
  tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
paseo_auto_accept() {
  python3 - "$1" <<'PY'
import glob, json, os, re, sys
aid = sys.argv[1]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", aid) or ".." in aid:
    print("invalid")
    sys.exit(0)
agents_dir = os.environ.get("PASEO_AGENTS_DIR") or os.path.join(os.path.expanduser("~"), ".config", "paseo", "agents")
fs = glob.glob(os.path.join(agents_dir, "*", f"{glob.escape(aid)}.json"))
if not fs:
    print("missing")
    sys.exit(0)
if len(fs) != 1:
    print("ambiguous")
    sys.exit(0)
try:
    with open(fs[0], encoding="utf-8") as fh:
        d = json.load(fh)
except (OSError, ValueError):
    print("unknown")
    sys.exit(0)
if not isinstance(d, dict):
    print("unknown")
    sys.exit(0)
cfg = d.get("config")
if cfg is not None and not isinstance(cfg, dict):
    print("unknown")
    sys.exit(0)
fv = cfg.get("featureValues") if isinstance(cfg, dict) else None
if fv is not None and not isinstance(fv, dict):
    print("unknown")
    sys.exit(0)
fv = fv or {}
features_raw = d.get("features")
if features_raw is not None and not isinstance(features_raw, list):
    print("unknown")
    sys.exit(0)
feats = [f for f in (features_raw or []) if isinstance(f, dict) and f.get("id") == "auto_accept"]
has_fv = "auto_accept" in fv
fv_val = fv.get("auto_accept")
if fv_val is True or any(f.get("value") is True for f in feats):
    print("on")
elif has_fv and fv_val is not False:
    print("unknown")
elif not feats:
    print("missing")
elif all(f.get("value") is False for f in feats):
    print("off")
else:
    print("unknown")
PY
}
# Permission guard: the imported agent must have Paseo ACP auto-accept OFF, otherwise abort (no permission expansion).
AA=$(paseo_auto_accept "$AID" 2>/dev/null || echo unknown)
log "auto_accept check for $AID: $AA"
if [ "$AA" != "off" ]; then log "ABORT: auto-accept not verifiably off -> archive import, restart tmux runner"; paseo archive "$AID" >>"$L" 2>&1; rm -f "$C/STOP"
  tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
echo "$AID" > "$C/.coord-agent"; paseo agent update "$AID" --name "Mesh coordinator (Kiro claude-opus-5.5 high)" >/dev/null 2>&1
log "imported as Paseo agent $AID"
rm -f "$C/STOP"
for i in $(seq 1 29); do
  for f in DONE.md BLOCKED.md PASEO-STOP; do [ -e "$C/$f" ] && { log "stop: $f present (agent stays visible in Paseo)"; exit 0; }; done
  AA=$(paseo_auto_accept "$AID" 2>/dev/null || echo unknown)
  [ "$AA" != "off" ] && { log "STOP: auto-accept is $AA on $AID; not sending further turns (needs James)"; exit 2; }
  log "paseo turn $i (auto_accept=off)"
  if ! paseo send "$AID" --prompt-file prompts/kiro-coordinator-continue.md >>"$L" 2>&1; then
    log "paseo send failed (turn $i)"; if [ "$i" = 1 ]; then log "fallback: restart tmux runner"; tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
    sleep 60; fi
  paseo wait "$AID" >>"$L" 2>&1; sleep 20
done
log "max turns reached"
