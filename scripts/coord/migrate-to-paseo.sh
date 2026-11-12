#!/usr/bin/env bash
# One-shot migration of the Kiro coordinator from the tmux runner to a native Paseo (kiro ACP) agent,
# at a safe boundary: waits until the tmux runner has finished its current turn and exited (STOP file),
# then imports the SAME Kiro session into Paseo and continues it there. Never runs two coordinators.
cd "$(dirname "$0")/../.."
C=docs/coordination; L=logs/raw/paseo-migrate.log; SID_FALLBACK="${MESH_PASEO_SESSION:-}"
log(){ echo "$(TZ=Asia/Shanghai date '+%F %T') $*" | tee -a "$L"; }
log "waiting for tmux runner kiro-coord to exit at turn boundary"
while tmux has-session -t kiro-coord 2>/dev/null || pgrep -f 'kiro-cli chat --no-interactive --agent mesh-coordinator' >/dev/null; do sleep 15; done
SID=$(cat "$C/.session" 2>/dev/null || echo "$SID_FALLBACK")
[ -n "$SID" ] || { log "no session id (docs/coordination/.session or MESH_PASEO_SESSION)"; exit 1; }
log "runner exited; session=$SID"
AID=$(paseo import "$SID" --provider kiro --cwd "$PWD" --label role=mesh-coordinator --json 2>>"$L" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("agentId",""))' 2>>"$L")
if [ -z "$AID" ]; then log "IMPORT FAILED -> restarting tmux runner (fallback)"; rm -f "$C/STOP"
  tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
# Permission guard: the imported agent must have Paseo ACP auto-accept OFF, otherwise abort (no permission expansion).
AA=$(python3 - "$AID" <<'PY'
import glob,json,os,sys
fs=glob.glob(os.path.expanduser(f"~/.paseo/agents/*/{sys.argv[1]}.json"))
if not fs: print("missing"); sys.exit()
d=json.load(open(fs[0]))
on=(d.get("config") or {}).get("featureValues",{}) or {}
feat=[f for f in d.get("features",[]) if f.get("id")=="auto_accept"]
print("on" if on.get("auto_accept") is True or any(f.get("value") is True for f in feat) else ("off" if feat else "missing"))
PY
)
log "auto_accept check for $AID: $AA"
if [ "$AA" != "off" ]; then log "ABORT: auto-accept not verifiably off -> archive import, restart tmux runner"; paseo archive "$AID" >>"$L" 2>&1; rm -f "$C/STOP"
  tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
echo "$AID" > "$C/.paseo-agent"; paseo agent update "$AID" --name "Mesh coordinator (Kiro claude-opus-5.5 high)" >/dev/null 2>&1
log "imported as Paseo agent $AID"
rm -f "$C/STOP"
for i in $(seq 1 29); do
  for f in DONE.md BLOCKED.md PASEO-STOP; do [ -e "$C/$f" ] && { log "stop: $f present (agent stays visible in Paseo)"; exit 0; }; done
  AA=$(python3 -c "import glob,json,os;d=json.load(open(glob.glob(os.path.expanduser('~/.paseo/agents/*/$AID.json'))[0]));print('on' if any(f.get('id')=='auto_accept' and f.get('value') is True for f in d.get('features',[])) or (d.get('config') or {}).get('featureValues',{}) and d['config']['featureValues'].get('auto_accept') is True else 'off')" 2>/dev/null || echo unknown)
  [ "$AA" != "off" ] && { log "STOP: auto-accept is $AA on $AID; not sending further turns (needs James)"; exit 2; }
  log "paseo turn $i (auto_accept=off)"
  if ! paseo send "$AID" --prompt-file prompts/kiro-coordinator-continue.md >>"$L" 2>&1; then
    log "paseo send failed (turn $i)"; if [ "$i" = 1 ]; then log "fallback: restart tmux runner"; tmux new-session -d -s kiro-coord -c "$PWD" "bash scripts/coord/kiro-coordinator.sh 2>&1 | tee -a logs/raw/kiro-coordinator-runner.log"; exit 1; fi
    sleep 60; fi
  paseo wait "$AID" >>"$L" 2>&1; sleep 20
done
log "max turns reached"
