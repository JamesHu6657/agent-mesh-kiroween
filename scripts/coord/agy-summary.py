import json,sys
for l in open(sys.argv[1]):
  try: d=json.loads(l)
  except: continue
  e=d.get("event")
  if e=="init": print("model",d["init"]["model"],"cwd",d["init"]["cwd"])
  elif e=="step_update":
    s=d["step_update"]
    if s.get("step_type")=="tool" and s["state"]=="DONE":
      p=s.get("tool_info",{}).get("parameters") or {}
      print("tool",s.get("tool_name"),str(p.get("CommandLine") or p.get("Path") or p.get("TargetFile") or "")[:110])
  elif e=="result":
    r=d["result"]; print("RESULT",r["status"],r.get("denied_actions"),r.get("usage"),"\n",r.get("response","")[-1500:])
