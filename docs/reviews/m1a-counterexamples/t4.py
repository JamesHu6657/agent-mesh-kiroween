exec(open("/tmp/m1a-review/bb.py").read())
print("H",H)
TE={"MESH_TESTING":"1"}
rc,j=m("init",user=True); check("init",rc==0,j)
# C4
os.chmod(H,0o755)
rc,j=m("daemon","run",timeout=10); check("C4 insecure home rc2 INSECURE_PERMISSIONS no socket", rc==2 and code(j)=="INSECURE_PERMISSIONS" and not os.path.exists(H+"/run/mesh.sock"),(rc,j))
rc,j=m("doctor","--fix-perms"); check("C4 doctor --fix-perms", rc==0 and oct(os.stat(H).st_mode&0o777)=="0o700",(rc,code(j)))
# C14 forward: install copied into $H/workspaces/inst
inst=H+"/workspaces/inst"; os.makedirs(inst)
shutil.copytree(REPO+"/bin",inst+"/bin"); shutil.copytree(REPO+"/mesh",inst+"/mesh",ignore=shutil.ignore_patterns("__pycache__"))
rc,j=m("daemon","run",binp=inst+"/bin/mesh",timeout=10); det=(j.get("error")or{}).get("details",{})
check("C14 daemon from install inside workspaces rc2 INSTALL_PATH_INSECURE + details, no socket", rc==2 and code(j)=="INSTALL_PATH_INSECURE" and det.get("install_root") and det.get("conflict") and not os.path.exists(H+"/run/mesh.sock"),(rc,j))
rc,j=m("doctor",binp=inst+"/bin/mesh"); check("C14 doctor reports INSTALL_PATH_INSECURE", "INSTALL_PATH_INSECURE" in json.dumps(j),(rc,j))
shutil.rmtree(inst)
# reverse: home inside install copy
inst2=T+"/inst2"; os.makedirs(inst2)
shutil.copytree(REPO+"/bin",inst2+"/bin"); shutil.copytree(REPO+"/mesh",inst2+"/mesh",ignore=shutil.ignore_patterns("__pycache__"))
H2=inst2+"/h"; os.makedirs(H2,mode=0o700)
fd=os.open(P,os.O_RDONLY); p=subprocess.run([inst2+"/bin/mesh","--home",H2,"--user","--passphrase-fd",str(fd),"init"],pass_fds=(fd,),capture_output=True,text=True,env=BASEENV); os.close(fd); print("init H2:",p.returncode,p.stdout[:150])
rc,j=m("daemon","run",home=H2,binp=inst2+"/bin/mesh",timeout=10); check("C14 reverse: MESH_HOME inside install root rejected", rc==2 and code(j)=="INSTALL_PATH_INSECURE",(rc,j))
# workspaces.root config into install root (daemon from inst2, home H outside)
rc,j=m("daemon","start",binp=inst2+"/bin/mesh",env=TE); check("daemon from inst2 with outside home starts", rc==0,(rc,j))
ws_in=inst2+"/ws"; os.makedirs(ws_in,mode=0o700)
rc,j=m("config","set","workspaces.root",json.dumps(ws_in),user=True,binp=inst2+"/bin/mesh"); check("workspaces.root inside install -> rc2 INSTALL_PATH_INSECURE", rc==2 and code(j)=="INSTALL_PATH_INSECURE",(rc,j))
os.chmod(T,0o700)
rc,j=m("config","set","workspaces.root",json.dumps(T),user=True,binp=inst2+"/bin/mesh"); check("workspaces.root containing install -> rc2 INSTALL_PATH_INSECURE", rc==2 and code(j)=="INSTALL_PATH_INSECURE",(rc,j))
m("daemon","stop",user=True,binp=inst2+"/bin/mesh"); time.sleep(0.3)
# PYTHONPATH: caller PYTHONPATH stdlib shadow
evil=T+"/evil"; os.makedirs(evil+"/mesh")
marker=T+"/SHADOW_HIT"
open(evil+"/argparse.py","w").write(f"open({marker!r},'a').write('argparse\\n')\nimport sys; del sys.modules['argparse']\nsys.path=[p for p in sys.path if p!={evil!r}]\nimport importlib; _m=importlib.import_module('argparse'); globals().update(_m.__dict__)\n")
open(evil+"/mesh/__init__.py","w").write(f"open({marker!r},'a').write('meshpkg\\n')\n")
rc,j=m("version",env={"PYTHONPATH":evil})
hit=open(marker).read() if os.path.exists(marker) else ""
check("mitigation 8: caller PYTHONPATH cannot shadow stdlib/mesh in bin/mesh", hit=="", (rc,hit))
if os.path.exists(marker): os.remove(marker)
# cwd shadow
cw=T+"/cwd"; os.makedirs(cw+"/mesh"); open(cw+"/mesh/__init__.py","w").write(f"open({marker!r},'a').write('cwd\\n')\n"); open(cw+"/json.py","w").write(f"open({marker!r},'a').write('cwdjson\\n')\n")
p=subprocess.run([BIN,"--home",H,"version"],cwd=cw,capture_output=True,text=True,env=BASEENV)
check("mitigation 8: cwd mesh/ and json.py do not shadow (-P)", not os.path.exists(marker),(p.returncode, open(marker).read() if os.path.exists(marker) else ""))
# usersite
# I7 tamper + doctor read-only
rc,j=m("daemon","start",env=TE); m("agent","create","--name","a1","--adapter","none","--delivery","inbox","--max-concurrency","1",user=True); m("daemon","stop",user=True); time.sleep(0.3)
dbp=H+"/mesh.db"; mt=os.stat(dbp).st_mtime_ns; h0=hashlib.sha256(open(dbp,"rb").read()).hexdigest()
rc,j=m("doctor","--check-invariants"); check("A8 clean invariants rc0 empty", rc==0, (rc,j))
check("doctor --check-invariants does not modify DB", h0==hashlib.sha256(open(dbp,"rb").read()).hexdigest())
c=sqlite3.connect(dbp); cols=[r[1] for r in c.execute("pragma table_info(events)")]; print("event cols",cols)
n=c.execute("select count(*) from events").fetchone()[0]; print("events",n)
c.execute("update events set payload_json='{\"tampered\":true}' where rowid=(select max(rowid) from events where rowid>1)"); c.commit(); c.close()
rc,j=m("doctor","--check-invariants"); check("I7 tampered event payload detected", rc!=0 and "I7" in json.dumps(j),(rc,json.dumps(j)[:300]))
c=sqlite3.connect(dbp); c.execute("delete from events where rowid=(select min(rowid)+1 from events)"); c.commit(); c.close()
rc,j=m("doctor","--check-invariants"); check("I7 deleted middle event detected", rc!=0 and "I7" in json.dumps(j),(rc,json.dumps(j)[:300]))
print("SUMMARY", sum(1 for r in results if r[1]), "pass /", len(results))
for r in results:
    if not r[1]: print("  FAIL:",r[0], str(r[2])[:300])
for d in (H,T): shutil.rmtree(d,ignore_errors=True)
