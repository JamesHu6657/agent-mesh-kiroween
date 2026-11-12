exec(open("/tmp/m1a-review/bb.py").read())
print("H",H)
rc,j=m("init",user=True); check("init rc0", rc==0, j)
rc,j=m("daemon","start",env={"MESH_TESTING":"1"}); check("daemon start (no --user, testing) rc0", rc==0, j)
pid=int(open(H+"/run/meshd.pid").read().strip())
# C1
ss=subprocess.run(["ss","-tunap"],capture_output=True,text=True).stdout
check("C1 no TCP/UDP owned by meshd", f"pid={pid}," not in ss, [l for l in ss.splitlines() if f"pid={pid}," in l])
ssx=subprocess.run(["ss","-xlp"],capture_output=True,text=True).stdout
check("C1 unix listen at $H/run/mesh.sock", H+"/run/mesh.sock" in ssx)
fds=os.listdir(f"/proc/{pid}/fd"); 
# C3
modes={p:oct(os.stat(p).st_mode&0o777) for p in [H,H+"/run",H+"/runs",H+"/workspaces",H+"/mesh.db",H+"/run/mesh.sock",H+"/logs/meshd.log"] if os.path.exists(p)}
check("C3 perms", all(v==("0o700" if os.path.isdir(p) else "0o600") for p,v in modes.items()) and len(modes)==7, modes)
# C5 second daemon run
rc,j=m("daemon","run",timeout=10); check("C5 second daemon run rc1 DAEMON_ALREADY_RUNNING", rc==1 and code(j)=="DAEMON_ALREADY_RUNNING",(rc,j))
rc,j=m("status"); check("C5 first still serving", rc==0 and j["data"]["pid"]==pid)
# agents
def mk(*a): return m("agent","create",*a,user=True)
rc,j=mk("--name","lead1","--adapter","none","--delivery","inbox","--can-lead","--max-concurrency","1"); check("create lead1",rc==0,j)
rc,j=mk("--name","grok","--adapter","none","--delivery","inbox","--capability","intake","--capability","relay","--max-concurrency","1"); check("create grok",rc==0,j)
rc,j=mk("--name","fx","--adapter","fake","--delivery","launch","--allowed-model","m-a","--allowed-model","m-b","--default-model","m-a","--max-concurrency","2"); check("create fx",rc==0,j)
# B10
rc,j=m("agent","show","fx",user=True); d=j.get("data",{}); check("B10 defaults edit/dev", d.get("default_profile")=="edit" and d.get("max_unapproved_profile")=="dev", d)
check("D1 models stored", d.get("default_model")=="m-a" and d.get("allowed_models")==["m-a","m-b"], d)
ver=d.get("version")
# tokens
def issue(name):
    rc,j=m("agent","token","issue",name,user=True); tok=j["data"]["token"]; f=os.path.join(T,name+".tok"); open(f,"w").write(tok); os.chmod(f,0o600); return tok,f
tl,TL=issue("lead1"); tg,TG=issue("grok")
rc,j=m("whoami",tok=TL); check("whoami lead1 token", rc==0, j)
# D5 / authz
rc,j=m("agent","update","fx","--allowed-model","m-z",tok=TL); check("D5 lead token model change rc4 CAPABILITY_MISSING", rc==4 and code(j)=="CAPABILITY_MISSING",(rc,j))
rc,j=m("agent","update","fx","--allowed-model","m-z",tok=TG); check("D5 grok(relay) model change rc4", rc==4 and code(j)=="CAPABILITY_MISSING",(rc,j))
rc,j=m("agent","update","fx","--allowed-model","m-z","--relayed","--user-statement","user approved",tok=TG); check("D5 relayed model change rc4", rc==4 and code(j)=="CAPABILITY_MISSING",(rc,j))
rc,j=m("agent","token","issue","lead1",tok=TG); check("agent cannot issue tokens rc4", rc==4,(rc,code(j)))
rc,j=m("agent","create","--name","x9","--adapter","none","--delivery","inbox","--max-concurrency","1",tok=TL); check("agent cannot create agent rc4", rc==4,(rc,code(j)))
rc,j=m("config","set","task.default_timeout_s","600",tok=TG); check("agent cannot config set rc4", rc==4 and code(j)=="CAPABILITY_MISSING",(rc,code(j)))
rc,j=m("daemon","stop",tok=TG); check("agent cannot daemon stop rc4", rc==4,(rc,code(j)))
rc,j=m("agent","disable","fx",tok=TL); check("agent cannot disable agent rc4", rc==4,(rc,code(j)))
rc,j=m("agent","token","list","lead1",tok=TL); check("agent cannot list tokens rc4", rc==4,(rc,code(j)))
rc,j=m("config","list",tok=TL); check("config.list by agent token: plan §7.3 says user-only (rc4)", rc==4,(rc,code(j)))
rc,j=m("config","list"); check("config.list anonymous should not succeed (plan §7.3 user-only)", rc!=0,(rc,code(j)))
rc,j=m("agent","list"); check("agent.list anonymous should not succeed (plan §7.3)", rc!=0,(rc,code(j), str(j)[:200]))
# B9
rc,j=m("agent","update","fx","--if-version","999","--max-concurrency","3",user=True); check("B9 VERSION_MISMATCH rc6", rc==6 and code(j)=="VERSION_MISMATCH",(rc,j))
rc,j=m("agent","show","fx",user=True); check("B9 unchanged", j["data"]["max_concurrency"]==2)
# B2
rc,j=m("agent","show","no-such-agent",user=True); check("B2 NOT_FOUND rc5", rc==5 and code(j)=="NOT_FOUND" and j.get("exit_code")==5,(rc,j))
# D8 flag values
vals=["0","-1","1.5","abc","","1e3","0x10","+3"," 2","9223372036854775808"]
bad=[]
for i,v in enumerate(vals):
    rc,j=m("agent","create","--name",f"c{i}","--adapter","fake","--allowed-model","m-a","--default-model","m-a","--max-concurrency",v,user=True)
    msg=(j.get("error") or {}).get("message")
    exp=f"max_concurrency must be a positive integer (1..9223372036854775807); got '{v}'"
    if not(rc==2 and code(j)=="INVALID_CONCURRENCY" and msg==exp): bad.append((v,rc,code(j),msg))
check("D8 flag values", not bad, bad)
bad=[]
for i,v in enumerate([True,"2",2.0,0,None,[1],-5]):
    body=json.dumps({"name":f"j{i}","adapter":"fake","allowed_models":["m-a"],"default_model":"m-a","max_concurrency":v})
    rc,j=m("agent","create","--input","-",stdin=body,user=True)
    if not(rc==2 and code(j)=="INVALID_CONCURRENCY"): bad.append((v,rc,code(j),(j.get("error") or {}).get("message")))
check("D8 JSON values (pure --input -)", not bad, bad)
bad=[]
for i,v in enumerate([True,"2",2.0,0,None,[1],-5]):
    rc,j=m("agent","create","--name",f"k{i}","--adapter","fake","--allowed-model","m-a","--default-model","m-a","--input","-",stdin=json.dumps({"max_concurrency":v}),user=True)
    if not(rc==2 and code(j)=="INVALID_CONCURRENCY"): bad.append((v,rc,code(j),(j.get("error") or {}).get("message")))
check("D8 JSON values (flags + --input max_concurrency)", not bad, bad)
db=sqlite3.connect(f"file:{H}/mesh.db?mode=ro",uri=True)
names=[r[0] for r in db.execute("select p.name from agents a join principals p on p.id=a.id")]
check("D8 no rows created", not any(n[:1] in "cjk" and n[1:].isdigit() for n in names), names)
# D9
bad=[]
for i,v in enumerate(["1","7","9223372036854775807"]):
    rc,j=m("agent","create","--name",f"ok{i}","--adapter","fake","--allowed-model","m-a","--default-model","m-a","--max-concurrency",v,user=True)
    rc2,j2=m("agent","show",f"ok{i}",user=True)
    if rc!=0 or str(j2.get("data",{}).get("max_concurrency"))!=v: bad.append((v,rc,j2.get("data",{}).get("max_concurrency")))
for i,v in enumerate([1,7,9223372036854775807]):
    body=json.dumps({"name":f"okj{i}","adapter":"fake","allowed_models":["m-a"],"default_model":"m-a","max_concurrency":v})
    rc,j=m("agent","create","--input","-",stdin=body,user=True)
    rc2,j2=m("agent","show",f"okj{i}",user=True)
    if rc!=0 or j2.get("data",{}).get("max_concurrency")!=v: bad.append((v,rc,code(j)))
check("D9 values flag+JSON", not bad, bad)
# D10
rc,j=m("agent","update","fx","--max-concurrency","0",user=True); check("D10 update 0", rc==2 and code(j)=="INVALID_CONCURRENCY",(rc,j))
rc,j=m("config","set","scheduler.max_total_runs","0",user=True); check("D10 config 0", rc==2 and code(j)=="INVALID_CONCURRENCY",(rc,j))
rc,j=m("config","set","scheduler.max_total_runs","true",user=True); check("max_total_runs bool rejected", rc==2,(rc,code(j)))
rc,j=m("config","set","scheduler.max_total_runs","null",user=True); check("max_total_runs null accepted", rc==0,(rc,code(j)))
# D2
rc,j=mk("--name","d2","--adapter","fake","--allowed-model","m-a","--default-model","m-z","--max-concurrency","1"); check("D2 DEFAULT_MODEL_NOT_ALLOWED", rc==2 and code(j)=="DEFAULT_MODEL_NOT_ALLOWED",(rc,j))
rc,j=mk("--name","d2b","--adapter","fake","--max-concurrency","1"); check("D2 launch agent no models INVALID_ARGUMENT param", rc==2 and code(j)=="INVALID_ARGUMENT" and j["error"]["details"].get("param")=="allowed_models",(rc,j))
# D17
rc,j=mk("--name","ag1","--adapter","agy","--allowed-model","gemini-3.8-flash-high","--default-model","gemini-3.8-flash-high","--allowed-effort","high","--default-effort","high","--max-concurrency","1")
check("D17 EFFORT_NOT_ALLOWED model_encodes_effort", rc==2 and code(j)=="EFFORT_NOT_ALLOWED" and j["error"]["details"].get("reason")=="model_encodes_effort",(rc,j))
rc,j=mk("--name","ag1","--adapter","agy","--allowed-model","gemini-3.8-flash-high","--default-model","gemini-3.8-flash-high","--max-concurrency","1"); check("D17 without effort rc0", rc==0,(rc,j))
# N2
for a in ["hermes","openclaw"]:
    rc,j=mk("--name","n"+a,"--adapter",a,"--allowed-model","x","--default-model","x","--max-concurrency","1"); check(f"N2 {a} ADAPTER_NOT_INSTALLED rc13", rc==13 and code(j)=="ADAPTER_NOT_INSTALLED",(rc,j))
# B13 config
rc,j=m("config","list",user=True); keys=set()
dd=j.get("data"); 
items = dd.get("settings") or dd.get("items") or dd
print("config list sample:", json.dumps(dd)[:400])
for k,v,expect in [("nosuch.key","1","INVALID_SETTING"),("task.default_timeout_s",'"600"',"INVALID_SETTING"),("task.default_timeout_s","0","INVALID_SETTING"),
                   ("task.default_timeout_s","600.0","INVALID_SETTING"),("task.default_timeout_s","true","INVALID_SETTING"),("task.default_timeout_s","604801","INVALID_SETTING"),
                   ("lead.default_agent",'"fx"',"INVALID_SETTING"),("lead.default_agent",'"nobody"',"INVALID_SETTING"),("lead.profile",'"godmode"',"INVALID_SETTING"),
                   ("task.default_max_attempts","11","INVALID_SETTING"),("approval.policy.config",'"relay_ok"',"INVALID_SETTING"),("notify.command",'[]',"INVALID_SETTING"),
                   ("task.default_timeout_s","NaN","INVALID_SETTING"),("task.default_timeout_s","1e3","INVALID_SETTING"),("lease.ttl_s","9"*400,"INVALID_SETTING"),
                   ("task.default_timeout_s","not json","INVALID_SETTING"),("workspaces.root",'"relative/path"',"INVALID_SETTING"),("workspaces.root",'"/nonexistent-xyz"',"INVALID_SETTING")]:
    rc,j=m("config","set",k,v,user=True)
    det=(j.get("error") or {}).get("details",{})
    check(f"B13 config set {k}={v[:20]} -> rc2 {expect} details.key", rc==2 and code(j)==expect and det.get("key")==k, (rc,code(j),det))
rc,j=m("config","set","lead.default_agent",'"lead1"',user=True); check("config lead.default_agent lead1 ok", rc==0,(rc,j))
rc,j=m("config","set","task.default_timeout_s","1",user=True); check("testing min 1 accepted in testing mode", rc==0,(rc,code(j)))
rc,j=m("config","set","task.default_timeout_s","600",user=True); check("B13 600 ok", rc==0,(rc,j))
rc,j=m("config","get","task.default_timeout_s",user=True); check("config get 600", "600" in json.dumps(j.get("data")),j)
# G17
rc,j=m("config","set","task.default_timeout_s","600","--user","--input","-",stdin=json.dumps({"passphrase":PASS})); check("G17 PASSPHRASE_SOURCE_REJECTED rc2", rc==2 and code(j)=="PASSPHRASE_SOURCE_REJECTED",(rc,j))
p=subprocess.run(["setsid","-w",BIN,"--home",H,"config","set","task.default_timeout_s","600","--user"],stdin=subprocess.DEVNULL,capture_output=True,text=True,env={**BASEENV,"MESH_PASSPHRASE":PASS},timeout=20)
try: jj=json.loads(p.stdout)
except Exception: jj={}
check("G17 NO_TTY rc3 env ignored", p.returncode==3 and code(jj)=="NO_TTY",(p.returncode,p.stdout[:200],p.stderr[-200:]))
# C10/C12 token file
f644=os.path.join(T,"g644.tok"); open(f644,"w").write(tg); os.chmod(f644,0o644)
rc,j=m("whoami",tok=f644); check("C10 0644 TOKEN_FILE_INSECURE rc3", rc==3 and code(j)=="TOKEN_FILE_INSECURE",(rc,j))
fw=H+"/workspaces/g.tok"; shutil.copy(TG,fw); os.chmod(fw,0o600)
rc,j=m("whoami",tok=fw); check("C12 workspaces token rc3", rc==3 and code(j)=="TOKEN_FILE_INSECURE",(rc,j))
fr=H+"/runs/g.tok"; shutil.copy(TG,fr); os.chmod(fr,0o600)
rc,j=m("whoami",tok=fr); check("token under runs rc3", rc==3 and code(j)=="TOKEN_FILE_INSECURE",(rc,j))
ln=os.path.join(T,"link.tok"); os.symlink(fw,ln)
rc,j=m("whoami",tok=ln); check("symlink to workspaces token rc3 (realpath)", rc==3 and code(j)=="TOKEN_FILE_INSECURE",(rc,j))
os.remove(fw); os.remove(fr)
rc,j=m("whoami",env={"MESH_TOKEN":tg}); check("MESH_TOKEN env works", rc==0 and j["data"].get("name","")=="grok" or "grok" in json.dumps(j),(rc,str(j)[:200]))
rc,j=m("whoami","--token-file",TG,stdin=None); 
# token revoke
rc,j=m("agent","token","list","grok",user=True); s=json.dumps(j); check("C9 token list has no secret", tg not in s and tg.split("_")[3] not in s,(s[:200]))
cred=db.execute("select lookup, length(secret_hash), typeof(secret_hash) from credentials where kind='agent_token'").fetchall(); check("C9 db only lookup+32-byte hash", all(r[1]==32 for r in cred), cred)
rc,j=m("agent","token","revoke", j["data"]["items"][0]["id"] if "items" in j.get("data",{}) else "x",user=True) if False else (None,None)
# disable agent -> token unusable
rc,j=m("agent","disable","grok",user=True); rc2,j2=m("whoami",tok=TG); check("disabled agent token rejected rc3", rc==0 and rc2==3,(rc,code(j),rc2,code(j2)))
m("agent","enable","grok",user=True)
# unimplemented verbs
bad=[]
for args,ms in [(["task","create","--project","p","--title","t"],"M1b"),(["intake","submit","--title","t","--goal","g","--criterion","c"],"M1b"),(["project","list"],"M1b"),
                (["approval","list"],"M1b"),(["inbox","list"],"M1b"),(["event","list"],"M1b"),(["task","start","t"],"M1c"),(["run","list"],"M1c"),(["task","merge","t"],"M2"),
                (["audit","verify"],None),(["workspace","list"],None),(["dep","add","a","b"],"M1b"),(["top"],None)]:
    rc,j=m(*args,tok=TL)
    det=(j.get("error") or {}).get("details",{})
    if not(rc==2 and code(j)=="NOT_IMPLEMENTED_IN_M1" and (ms is None or det.get("milestone")==ms)): bad.append((args[:2],rc,code(j),det.get("milestone")))
check("NOT_IMPLEMENTED_IN_M1 with correct milestone", not bad, bad)
# B7
rc1,_=m("agent","show","nope",user=True); rc2,j2=m("agent","show","nope","--format","text",user=True); check("B7 identical rc", rc1==rc2==5,(rc1,rc2))
# C11 + secret grep
m("daemon","stop",user=True)
time.sleep(0.5)
hits=[]
for root,_,files in os.walk(H):
    for fn in files:
        b=open(os.path.join(root,fn),"rb").read()
        for s in [PASS,tl,tg,tl.split("_")[3],tg.split("_")[3]]:
            if s.encode() in b: hits.append((fn,s[:12]))
check("secrets not in $H (passphrase, tokens)", not hits, hits)
log=open(H+"/logs/meshd.log").read(); check("C11 no \"auth\" in log", '"auth"' not in log, log.count('"auth"'))
open("/tmp/m1a-review/state2.json","w").write(json.dumps({"H":H,"T":T}))
print("SUMMARY", sum(1 for r in results if r[1]), "pass /", len(results))
for r in results:
    if not r[1]: print("  FAIL:",r[0])
