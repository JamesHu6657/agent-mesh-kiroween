# Isolated command walkthrough

Run this from an Agent Mesh checkout on Linux with Python 3.11+ and git. It uses the
checkout's CLI, a fresh temporary HOME, separate XDG directories, a new MESH_HOME and
an independent Unix socket. No TCP listener, real Mesh daemon, real token or vendor
inference is needed. The passphrase and all issued credentials are dummy fixtures;
they are never printed. The temporary daemon is stopped and its complete fixture
root removed in `finally`. Only non-secret command/return-code metadata remains in
`docs-audit-result.json` (remove it when done).

`task start` returns the run's client `session`; the walkthrough exports it as
`MESH_SESSION` for that run's heartbeat and submit, as a worker shell would.

The code exercises setup/idempotency; authentication; inbox list/wait/ack; task
start/heartbeat/submit/accept; project completion; config and diagnostics; adapter
metadata/probes; and a local git worktree/merge/deliver/cleanup flow. It uses a local
dummy repo, never a hosted repository. Run administrative commands only inside this
isolated walkthrough, not as a worker on a live home.

Copy the following Python into a temporary file and run `python3 /path/to/that-file.py`
from the checkout root. Expected result: `CLEANED True TOTAL 65 UNEXPECTED 0`.
Adapter probes inspect a sanitized PATH without vendor executables; they validate
Mesh's missing-CLI behavior, not real vendor login, model availability or inference.
Package-manager installation alternatives and vendor inference are intentionally not
performed by this walkthrough. Use the per-adapter smoke evidence for tested vendor
versions; do not describe those as newly verified by this audit.

```python
import os, json, subprocess, tempfile, pathlib, time, shutil
repo=pathlib.Path.cwd().resolve()
assert (repo/"bin/mesh").is_file(), "Run from the Agent Mesh checkout root"
root=pathlib.Path(tempfile.mkdtemp(prefix='mh-docs-')); home=root/'home';home.mkdir(); mh=root/'mesh'; tok=root/'tokens';tok.mkdir(mode=0o700); p=root/'pass';p.write_text('documentation-only-test-passphrase\n');p.chmod(0o600)
env={k:v for k,v in os.environ.items() if not k.startswith(('MESH_','XDG_'))};env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),XDG_CACHE_HOME=str(home/'.cache'),MESH_HOME=str(mh),MESH_PASSPHRASE_FILE=str(p),PATH=str(repo/'bin')+':/usr/bin:/bin')
rows=[]
def run(label,args,auth='user',expected=(0,),stdin=None):
    cmd=[str(repo/'bin/mesh'),'--home',str(mh)]+args
    if auth=='user':cmd+=['--user','--passphrase-file',str(p)]
    elif auth:cmd+=['--token-file',str(auth)]
    cp=subprocess.run(cmd,env=env,cwd=repo,text=True,input=stdin,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    try:b=json.loads(cp.stdout)
    except:b={}
    row={'label':label,'argv':args,'rc':cp.returncode,'expected':list(expected),'ok':cp.returncode in expected,'error':b.get('error',{}),'data_keys':list(b.get('data',{})) if isinstance(b.get('data'),dict) else []};rows.append(row)
    print(label,cp.returncode,row['error'].get('code','OK'),flush=True)
    (root/'audit.json').write_text(json.dumps(rows,indent=2))
    if cp.returncode not in expected:print(cp.stdout[:1800],cp.stderr[:300],flush=True)
    return b.get('data',{}),b
try:
    print('ISOLATED_ROOT',root,flush=True)
    d,_=run('setup',['setup','--token-dir',str(tok)],None); lead=pathlib.Path(d['token_file'])
    assert str(lead).startswith(str(root)) and lead.stat().st_mode&0o777==0o600
    assert str(mh)!='/home/user/.agent-mesh'
    run('setup-idempotent',['setup','--token-dir',str(tok)],None)
    run('version',['version'],None)
    run('daemon-status',['daemon','status'],None)
    run('status',['status'],lead)
    run('whoami',['whoami'],lead)
    run('doctor',['doctor'],None)
    run('doctor-fix-perms',['doctor','--fix-perms'],None)
    for topic in ['auth','approvals','workflow','adapters','exit-codes']:run('help-'+topic,['help',topic],None)
    run('agent-create',['agent','create','--name','worker1','--adapter','none','--delivery','inbox','--max-concurrency','1'])
    d,_=run('worker-token',['agent','token','issue','worker1']);worker=tok/'worker1.token';worker.write_text(d['token']+'\n');worker.chmod(0o600)
    d,_=run('intake',['intake','submit','--title','Hello','--goal','Write hello.txt','--criterion','hello.txt exists']);print('INTAKE_KEYS',list(d),flush=True);prj=d.get('project_id') or d.get('id')
    if not prj and 'project' in d:prj=d['project']['id']
    pd,_=run('project-show',['project','show',prj]);run('inbox-list',['inbox','list'],lead)
    d,_=run('task-create',['task','create','--project',prj,'--title','Write hello.txt','--spec','Create hello.txt','--assignee','worker1'],lead);tsk=d.get('task_id') or d.get('id')
    if not tsk and 'task' in d:tsk=d['task']['id']
    run('inbox-wait',['inbox','wait','--timeout','2s'],worker,expected=(0,8))
    msgs,_=run('worker-inbox',['inbox','list'],worker)
    for m in msgs.get('items',[]):run('inbox-ack',['inbox','ack',m['id']],worker)
    d,_=run('task-start',['task','start',tsk],worker);runid=d['run_id'];fence=str(d['fence']);ws=pathlib.Path(d['workspace']);(ws/'hello.txt').write_text('Hello\n')
    env['MESH_SESSION']=d['session']  # the run's session (task start prints it); run writes must send it
    run('heartbeat',['task','heartbeat',tsk,'--run',runid,'--fence',fence],worker)
    run('submit',['task','submit',tsk,'--run',runid,'--fence',fence,'--summary','hello.txt exists'],worker)
    env.pop('MESH_SESSION',None);run('accept',['task','accept',tsk],lead)
    print('CRITERIA',pd.get('criteria'),flush=True)
    criteria=pd.get('criteria',[]);report={'criteria':[{'id':x['id'],'met':True,'evidence':'hello.txt exists'} for x in criteria]}
    run('complete',['project','complete',prj,'--report','-'],lead,stdin=json.dumps(report))
    run('delegated-mode',['config','set','approval.mode','"delegated"'])
    run('strict-mode',['config','set','adapter.evidence_mode','"strict"'])
    run('sensitive-policy',['config','set','approval.policy.deploy','"direct_only"'])
    run('pending',['approval','pending'],lead)
    run('audit',['audit','verify'])
    run('event-list',['event','list'])
    for a in ['kiro','droid','agy','vibe','hermes','openclaw']:
        run('show-'+a,['adapter','show',a],lead)
        # Use a sanitized PATH and HOME: no account state or vendor inference.
        run('probe-'+a,['adapter','probe',a],expected=(0,13))
        run('models-'+a,['adapter','models',a],expected=(0,2,13))
    # repo workflow with an entirely disposable, local repository.
    fixture=root/'calculator';fixture.mkdir();
    def git(*args,cwd=fixture):
        cp=subprocess.run(['git',*args],cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE);assert cp.returncode==0,(args,cp.stderr);return cp.stdout.strip()
    git('init','-b','main');git('-c','user.name=Docs Fixture','-c','user.email=docs@example.invalid','commit','--allow-empty','-m','fixture')
    d,_=run('repo-intake',['intake','submit','--title','Calculator','--goal','Fix addition','--criterion','Tests pass','--lead','lead','--repo',str(fixture),'--base-branch','main','--test-command','["python3","-c","print(123)"]']);rp=d.get('project_id') or d.get('id')
    d,_=run('repo-task-create',['task','create','--project',rp,'--title','Add file','--spec','Create hello.txt','--assignee','worker1'],lead);rt=d.get('task_id') or d.get('id')
    env.pop('MESH_SESSION',None)  # a new task start gets its own session
    d,_=run('repo-task-start',['task','start',rt],worker);env['MESH_SESSION']=d['session'];r=d['run_id'];f=str(d['fence']);w=pathlib.Path(d['workspace']);(w/'hello.txt').write_text('hello\n');git('add','hello.txt',cwd=w);git('-c','user.name=Docs Fixture','-c','user.email=docs@example.invalid','commit','-m','docs fixture',cwd=w)
    run('repo-submit',['task','submit',rt,'--run',r,'--fence',f,'--summary','committed file'],worker);env.pop('MESH_SESSION',None);run('repo-accept',['task','accept',rt],lead)
    # Repo-local identity here is a disposable fixture, not a real account/config change.
    git('config','user.name','Docs Fixture');git('config','user.email','docs@example.invalid')
    run('merge',['task','merge',rt],lead);run('deliver',['project','deliver',rp],lead)
    run('prune-preview',['workspace','prune','--project',rp,'--dry-run'],lead);run('prune',['workspace','prune','--project',rp],lead)
    run('delete-already-pruned',['workspace','delete',rt],expected=(0,5))
    run('daemon-stop',['daemon','stop'])
    run('doctor-invariants',['doctor','--check-invariants'],None)
    run('daemon-restart',['daemon','start'],None)
finally:
    try:run('cleanup-stop',['daemon','stop'])
    except Exception as e:print('cleanup error',type(e).__name__,flush=True)
    # Only write non-secret audit metadata outside the temporary home.
    pathlib.Path('/tmp/doc-audit-result.json').write_text(json.dumps({'root':str(root),'rows':rows},indent=2))
    shutil.rmtree(root)
    print('CLEANED',not root.exists(),'TOTAL',len(rows),'UNEXPECTED',sum(not r['ok'] for r in rows),flush=True)
```
