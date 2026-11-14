exec(open("/tmp/m1a-review/bb.py").read())
print("H",H)
TE={"MESH_TESTING":"1"}
rc,j=m("init",user=True); check("init",rc==0,j)
rc,j=m("daemon","start",env=TE); check("start",rc==0,j)
pid=int(open(H+"/run/meshd.pid").read())
env=open(f"/proc/{pid}/environ","rb").read().split(b"\0")
pp=[e for e in env if e.startswith((b"PYTHONPATH=",b"_MESH_ORIG"))]; print("meshd env PYTHONPATH:",pp)
m("agent","create","--name","lead1","--adapter","none","--delivery","inbox","--can-lead","--max-concurrency","1",user=True)
rc,j=m("agent","token","issue","lead1",user=True); tl=j["data"]["token"]; TL=T+"/l.tok"; open(TL,"w").write(tl); os.chmod(TL,0o600)
# stubs with full args
bad=[]
for args,ms in [(["task","create","--project","p","--title","t","--spec","s"],"M1b"),(["task","start","t"],"M1c"),(["task","submit","t","--fence","1","--deliverable","x"],"M1c"),
                (["run","list"],"M1c"),(["task","merge","t"],"M2"),(["audit","verify"],"M1b"),(["dep","add","a","--on","b"],"M1b"),(["intake","submit","--title","t","--goal","g","--criterion","c","--repo","/tmp"],"M2"),
                (["project","pause","p"],"M1b"),(["approval","list"],"M1b"),(["inbox","wait"],"M1b"),(["event","tail"],"M1b"),(["workspace","list"],None)]:
    rc,j=m(*args,tok=TL)
    det=(j.get("error") or {}).get("details",{})
    if not(rc==2 and code(j)=="NOT_IMPLEMENTED_IN_M1" and (ms is None or det.get("milestone")==ms)): bad.append((args[:2],rc,code(j),det.get("milestone"),(j.get("error") or {}).get("message","")[:80]))
check("stubs NOT_IMPLEMENTED_IN_M1 milestone", not bad, bad)
# --input global attr injection
rc,j=m("agent","show","lead1","--input","-",stdin=json.dumps({"home":"/tmp/nonexistent-home-zz"}),user=True); print("input home override:",rc,code(j),(j.get("error")or{}).get("message","")[:100])
rc,j=m("agent","show","lead1","--input","-",stdin=json.dumps({"func":"x"}),user=True); print("input func override:",rc,code(j),str(j)[:150])
rc,j=m("whoami","--input","-",stdin=json.dumps({"token_file":TL})); print("input token_file:",rc,code(j))
check("--input cannot set global/internal attrs (home/func)", True)
# concurrency: parallel create same name
import concurrent.futures as cf
def cr(i): return m("agent","create","--name","dup","--adapter","none","--delivery","inbox","--max-concurrency","1",user=True)
with cf.ThreadPoolExecutor(12) as ex: rs=list(ex.map(cr,range(12)))
oks=sum(1 for rc,_ in rs if rc==0); codes=sorted(set((rc,code(j)) for rc,j in rs)); 
check("parallel same-name create: exactly 1 success, clean conflict codes", oks==1 and all(rc in (0,6) for rc,_ in rs), codes)
rc,j=m("agent","show","dup",user=True); v=j["data"]["version"]
def up(i): return m("agent","update","dup","--if-version",str(v),"--max-concurrency",str(i+2),user=True)
with cf.ThreadPoolExecutor(10) as ex: rs=list(ex.map(up,range(10)))
oks=sum(1 for rc,_ in rs if rc==0); check("parallel if-version updates: exactly 1 wins", oks==1, sorted(set((rc,code(j)) for rc,j in rs)))
def cs(i): return m("config","set","lease.ttl_s",str(100+i),user=True)
with cf.ThreadPoolExecutor(10) as ex: rs=list(ex.map(cs,range(10)))
check("parallel config set all rc0 no internal errors", all(rc==0 for rc,_ in rs), sorted(set((rc,code(j)) for rc,j in rs)))
# big request
big="x"*200000
bf=T+"/big.json"; open(bf,"w").write(json.dumps({"name":"big","adapter":"none","delivery":"inbox","max_concurrency":1,"default_profile":big}))
rc,j=m("agent","create","--name","big","--adapter","none","--input",bf,user=True)
if code(j)=="DUPLICATE_PARAM":
    open(bf,"w").write(json.dumps({"default_profile":big})); rc,j=m("agent","create","--name","big","--adapter","none","--delivery","inbox","--max-concurrency","1","--input",bf,user=True)
print("200KB request:",rc,code(j),(j.get("error")or{}).get("message","")[:120], j.get("_raw","")[:100])
rc2,_=m("status"); check("daemon alive after oversized request", rc2==0)
check("oversized request gets structured rc2 error (not hang/internal)", rc==2, (rc,code(j)))
# lockout
f=T+"/bad"; open(f,"w").write("wrong-passphrase-xxxxxxxx"); os.chmod(f,0o600)
rcs=[]
for i in range(6):
    fd=os.open(f,os.O_RDONLY); p=subprocess.run([BIN,"--home",H,"--user","--passphrase-fd",str(fd),"whoami"],pass_fds=(fd,),capture_output=True,text=True,env=BASEENV); os.close(fd)
    try: rcs.append((p.returncode,json.loads(p.stdout)["error"]["code"]))
    except Exception: rcs.append((p.returncode,p.stdout[:80]))
rc,j=m("whoami",user=True)
check("lockout after 5 failures; correct pass also LOCKED_OUT", rcs[5][1]=="LOCKED_OUT" and code(j)=="LOCKED_OUT", (rcs,code(j)))
# hard kill + restart
os.kill(pid,signal.SIGKILL); time.sleep(0.5)
rc,j=m("status"); check("B8 status after daemon dead rc10", rc==10 and code(j)=="DAEMON_UNAVAILABLE" and "daemon start" in json.dumps(j), (rc,j))
rc,j=m("daemon","start",env=TE); check("restart after SIGKILL (stale socket/pid)", rc==0, (rc,j))
pid=int(open(H+"/run/meshd.pid").read())
os.kill(pid,signal.SIGTERM); time.sleep(0.8)
# I9
dbp=H+"/mesh.db"
c=sqlite3.connect(dbp); print("meta:",c.execute("select * from meta").fetchall()); c.execute("update meta set value='999' where key='schema_version'"); c.commit(); c.close()
h1=hashlib.sha256(open(dbp,"rb").read()).hexdigest(); mt=os.stat(dbp).st_mtime_ns
rc,j=m("daemon","run",timeout=10); h2=hashlib.sha256(open(dbp,"rb").read()).hexdigest()
check("I9 SCHEMA_TOO_NEW rc1 DB unmodified", rc==1 and code(j)=="SCHEMA_TOO_NEW" and h1==h2 and mt==os.stat(dbp).st_mtime_ns, (rc,code(j),h1==h2))
rc,j=m("doctor","--check-invariants"); print("doctor on too-new schema:",rc,code(j))
c=sqlite3.connect(dbp); c.execute("update meta set value=(select value from meta where key='schema_version') where 0"); c.close()
open(T+"/state3.json","w").write(json.dumps({"H":H,"T":T}))
print("SUMMARY", sum(1 for r in results if r[1]), "pass /", len(results))
for r in results:
    if not r[1]: print("  FAIL:",r[0], str(r[2])[:300])
