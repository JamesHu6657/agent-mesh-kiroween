import os, sys, json, subprocess, tempfile, time, shutil, sqlite3, stat, hashlib, glob, signal
REPO="/workspace/mesh-fresh-v1"
BIN=REPO+"/bin/mesh"
H=tempfile.mkdtemp(prefix="mr-", dir="/tmp"); os.chmod(H,0o700)
T=tempfile.mkdtemp(prefix="mrt-", dir="/tmp"); os.chmod(T,0o700)
PASS="review-passphrase-0123456789xyz"
P=os.path.join(T,"p"); open(P,"w").write(PASS); os.chmod(P,0o600)
TRIP=os.path.join(T,"trip.log")
BASEENV=dict(os.environ); BASEENV["PATH"]=REPO+"/tests/shims:"+BASEENV["PATH"]; BASEENV["MESH_TRIPWIRE_FILE"]=TRIP
for k in list(BASEENV):
    if k.startswith("MESH_") and k!="MESH_TRIPWIRE_FILE": del BASEENV[k]
results=[]
def m(*args, user=False, tok=None, stdin=None, env=None, home=H, binp=BIN, timeout=20):
    cmd=[binp,"--home",home]; fds=()
    fd=None
    if user:
        fd=os.open(P,os.O_RDONLY); cmd+=["--user","--passphrase-fd",str(fd)]; fds=(fd,)
    if tok:
        cmd+=["--token-file",tok]
    cmd+=list(args)
    e=dict(BASEENV); e.update(env or {})
    try:
        p=subprocess.run(cmd,input=stdin,text=True,capture_output=True,env=e,pass_fds=fds,timeout=timeout,stdin=None if stdin is not None else subprocess.DEVNULL)
    finally:
        if fd is not None: os.close(fd)
    try: j=json.loads(p.stdout)
    except Exception: j={"_raw":p.stdout[:400],"_err":p.stderr[-400:]}
    return p.returncode,j
def code(j): return (j.get("error") or {}).get("code")
def check(name, cond, info=""):
    results.append((name, bool(cond), info)); print(("PASS " if cond else "FAIL ")+name+("  :: "+str(info)[:300] if not cond or os.environ.get("V") else ""))
