#!/usr/bin/env python3
"""Run immutable, ledger-backed iterations over real offline AutoForge task contracts.

Each round is a distinct transaction, not model-weight learning. Rust's signed ledger
remains authoritative for promotion; runner checkpoints only orchestrate/reconcile it.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, time, traceback, uuid
from pathlib import Path
import bootstrap, recovery, external_evaluation, repair_protocol, paths
from universal.machine import _atomic_create
PROJECT = Path(__file__).resolve().parents[1]
EXE = paths.release_binary(PROJECT)
DEFAULT_SEED = 0xA170

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
def contract_bytes(task):
    return canonical({k:v for k,v in task.items() if k!="_runner_oracle"})
def digest(data): return hashlib.sha256(data).hexdigest()
def file_digest(path): return digest(path.read_bytes())
def source_hashes():
    sources = [Path(__file__), PROJECT/'bootstrap.py', PROJECT/'recovery.py', EXE, PROJECT/'Cargo.lock',
             PROJECT/'src/main.rs',PROJECT/'src/persistence.rs',PROJECT/'src/sandbox.rs',
             PROJECT/'providers/python_agent.py',PROJECT/'providers/julia_agent.jl',PROJECT/'providers/protocol.jl',
             PROJECT/'providers/Manifest.toml',PROJECT/'universal/machine.py',
             PROJECT/'external_evaluation.py',PROJECT/'external_fit.py',PROJECT/'repair_protocol.py']
    julia = paths.julia_binary(PROJECT)
    if julia.is_file(): sources.append(julia)
    return {p.relative_to(PROJECT).as_posix(): file_digest(p) for p in sources if p.is_file()}

def task_defs(spec):
    if spec is None: return bootstrap.functions()
    if not isinstance(spec, list) or not spec: raise ValueError("--tasks JSON must be a non-empty list")
    out=[]
    for i, item in enumerate(spec):
        if isinstance(item, str):
            p=Path(item); p=p.resolve() if p.is_absolute() else (Path.cwd()/p).resolve()
            data=json.loads(p.read_text(encoding="utf-8")); out.append((str(data.get("id",p.stem)),data.get("goal",""),data))
        elif isinstance(item,dict): out.append((str(item.get("id",f"task-{i+1}")),item.get("goal",""),item))
        else: raise ValueError("each task must be a contract object or task-file path")
    return out

def frozen_tasks(defs, rounds, seed, proposal_rounds):
    out=[]; oracles=[]
    for round_no in range(1, rounds+1):
        ix=(round_no-1)%len(defs); name,goal,source=defs[ix]
        ident=f"iter-{round_no:06d}-{re.sub(r'[^A-Za-z0-9_.-]','-',name)[:80]}"
        if isinstance(source,dict):
            task=dict(source); task.update(schema=1,id=ident,max_rounds=proposal_rounds); task.setdefault("goal",goal)
            if "public_cases" not in task or "holdout_cases" not in task: raise ValueError(f"contract {name!r} lacks cases")
            oracles.append(None)
        else:
            public,holdout=bootstrap.contract_cases(source,seed+ix)
            task={"schema":1,"id":ident,"goal":goal,"public_cases":public,"holdout_cases":holdout,"max_rounds":proposal_rounds}
            oracles.append(name)
        out.append(task)
    return out,oracles
class Runner:
    def __init__(self, run_dir, cfg, tasks, timeout):
        self.run_dir,self.cfg,self.tasks,self.timeout=run_dir,cfg,tasks,timeout
        self.state=run_dir/"state"; self.events=run_dir/"events"; self.checkpoints=run_dir/"checkpoints"
        self.rows=[]; self.last_checkpoint=None
        self.env=os.environ.copy()
        for k in tuple(self.env):
            if k.startswith("AUTOFORGE_"): self.env.pop(k)
        self.env.update(AUTOFORGE_PROJECT=str(PROJECT),AUTOFORGE_PROVIDER="offline",AUTOFORGE_PYTHON=sys.executable)
        julia=paths.julia_binary(PROJECT)
        if julia.is_file(): self.env["AUTOFORGE_JULIA"]=str(julia)
    def call(self,*args,expected=0):
        cmd=[str(EXE),*(str(a) for a in args)]
        p=subprocess.run(cmd,cwd=PROJECT,env=self.env,capture_output=True,timeout=self.timeout,check=False)
        out=p.stdout.decode("utf-8","replace").strip(); err=p.stderr.decode("utf-8","replace").strip()
        (self.events/f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json").write_bytes(canonical({"command":cmd,"returncode":p.returncode,"stdout":out,"stderr":err})+b"\n")
        if p.returncode!=expected: raise RuntimeError(f"command failed ({p.returncode}, expected {expected}): {cmd}\n{out}\n{err}")
        try:return json.loads(out or err)
        except json.JSONDecodeError:
            if expected:return {"raw":out or err}
            raise RuntimeError(f"command returned non-JSON: {cmd}: {out}\n{err}")
    def ledger(self):
        paths=sorted((self.state/"ledger").glob("*.json")); out=[]
        for n,p in enumerate(paths):
            if p.name!=f"{n:08}.json":raise ValueError("Rust ledger gap or invalid filename")
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out
    def active(self): return self.call("status",self.state).get("active")
    def state_root(self):
        key=self.state/"public.key"; charter=self.state/"charter.txt"
        if not key.is_file() or not charter.is_file():raise ValueError("missing Rust state identity files")
        return digest(canonical({"public_key":file_digest(key),"charter":file_digest(charter)}))
    def checkpoint(self,status,completed,detail=None):
        previous=sorted(self.checkpoints.glob("*.json")); seq=len(previous)
        payload={"schema":1,"sequence":seq,"status":status,"target_rounds":self.cfg["rounds"],
          "completed_rounds":completed,"config_sha256":self.cfg["config_sha256"],"source_hashes":self.cfg["source_hashes"],
          "state_root_sha256":self.cfg["state_root_sha256"],"ledger_count":len(self.ledger()),"active_release":self.active(),
          "rows_sha256":digest(canonical(self.rows)),"rows":self.rows,
          "previous_checkpoint_sha256":file_digest(previous[-1]) if previous else None,"recorded_at_unix":time.time(),"detail":detail or {}}
        _atomic_create(self.checkpoints/f"{seq:08}.json",canonical(payload)+b"\n"); self.last_checkpoint=payload
    def validate_checkpoints(self):
        paths=sorted(self.checkpoints.glob("*.json")); parent=None; rows=[]; completed=0
        for n,p in enumerate(paths):
            if p.name!=f"{n:08}.json":raise ValueError("checkpoint sequence gap")
            c=json.loads(p.read_text(encoding="utf-8"))
            if p.read_bytes()!=canonical(c)+b"\n":raise ValueError("checkpoint encoding changed")
            if c.get("sequence")!=n or c.get("previous_checkpoint_sha256")!=parent:raise ValueError("checkpoint chain mismatch")
            if c.get("config_sha256")!=self.cfg["config_sha256"] or c.get("source_hashes")!=self.cfg["source_hashes"]:raise ValueError("resume refused: config/source changed")
            if c.get("state_root_sha256")!=self.cfg["state_root_sha256"]:raise ValueError("resume refused: state root changed")
            if c.get("rows_sha256")!=digest(canonical(c.get("rows",[]))):raise ValueError("checkpoint rows digest mismatch")
            parent=file_digest(p); rows=c.get("rows",[]); completed=c.get("completed_rounds",0)
        if completed!=len(rows):raise ValueError("checkpoint counter/rows mismatch")
        return completed,rows
    def release_evidence(self,rid):
        raw=(self.state/"blobs"/rid).read_bytes(); env=json.loads(raw); payload=env["payload"]; blobs={}
        for field in ("wasm","wat","audit_wasm","task"):
            ident=payload[field]; data=(self.state/"blobs"/ident).read_bytes()
            if digest(data)!=ident:raise ValueError(f"artifact hash mismatch: {field}")
            blobs[field]={"id":ident,"sha256":ident,"bytes":len(data)}
        task=json.loads((self.state/"blobs"/payload["task"]).read_text(encoding="utf-8"))
        return {"payload":payload,"blobs":blobs,"task":task}
    def probes(self,task,index):
        cases=task["public_cases"]+task["holdout_cases"]
        oracle=self.cfg["task_oracles"][index]
        known={name:fn for name,_,fn in bootstrap.functions()}
        if oracle in known:
            inputs={tuple(c["input"]) for c in cases}
            probes=bootstrap.fresh_probes(known[oracle],self.cfg["seed"]+index,inputs)
        else:
            probes=[{"input":c["input"],"expected":c["expected"],"in_contract":True} for c in cases[:5]]
        for probe in probes:
            actual=self.call("execute",self.state,*probe["input"])
            if actual.get("output")!=probe["expected"]:raise ValueError(f"probe mismatch: {probe}, {actual}")
        return probes
    def verify_release(self,task,rid,previous,result):
        evidence=self.release_evidence(rid); payload=evidence["payload"]; attempts=payload.get("attempts",[])
        promotion_round=max(a['round'] for a in attempts)
        passes={line:[a for a in attempts if a.get('round')==promotion_round and a.get('line')==line and a.get('decision')=='PASS'] for line in ('python','julia')}
        if any(len(v)!=1 for v in passes.values()):raise ValueError(f"{task['id']}: expected one passing attempt per provider")
        gates=payload.get("gates",{}); required=("oracle","dual_line","shadow_replay","mutation_detected")
        if any(gates.get(k) is not True for k in required) or (previous is not None and gates.get("previous_replayed") is not True):raise ValueError("signed release gates incomplete")
        replay=self.call("replay",self.state)
        if replay.get("verified_releases",0)<len(self.rows)+1:raise ValueError("signed ledger replay count too small")
        return {"round":len(self.rows)+1,"task_id":task["id"],"task_sha256":payload["task"],"decision":result.get("decision"),
          "proposal_rounds":result.get("rounds"),"feedback_rounds":sorted({a.get("round") for a in attempts if a.get("public_failures")}),
          "previous_release":previous,"release":rid,"line_gate_evidence":{line:[{k:a.get(k) for k in ("round","wasm","wat","strategy","decision")} for a in rows] for line,rows in passes.items()},
          "signed_gates":gates,"release_artifacts":evidence["blobs"],"replay":replay,"fresh_probes":self.probes(task,len(self.rows))}
    def evaluate(self, task, rid, round_no, revision):
        if not self.cfg["evaluator_command"]: return None
        task_path=self.run_dir/"tasks"/f"{round_no:06}-r{revision:02}.json"
        task_path.parent.mkdir(exist_ok=True)
        raw=canonical(task)+b"\n"
        if task_path.exists():
            if task_path.read_bytes()!=raw:raise ValueError("immutable task revision changed")
        else:_atomic_create(task_path,raw)
        out=self.run_dir/"evaluations"/f"{round_no:06}-r{revision:02}"
        out.mkdir(parents=True,exist_ok=True)
        identity=external_evaluation.command_identity(self.cfg["evaluator_command"],PROJECT)
        if identity!=self.cfg["evaluator_identity"]:raise RuntimeError("external evaluator identity changed since run creation")
        result=external_evaluation.evaluate(PROJECT,self.state,task_path,rid,out,self.cfg["evaluator_command"],self.cfg["evaluation_timeout"])
        result["effective_task_sha256"]=digest(canonical(task));result["effective_task_path"]=str(task_path)
        return result
    def rollback_exact(self, rid):
        self.call('rollback',self.state,rid)

    def commit_round(self,row):
        # A successful evaluator cannot authorize a different or already-stopped candidate.
        self.call('status',self.state)  # Verify signed history before taking file locks.
        with recovery.lock(self.state/'control.lock'), recovery.lock(self.state/'commit.lock'):
            if (self.state/'STOP').exists():raise RuntimeError('operator stop before round commit')
            current=None
            for entry in self.ledger():
                if 'active' in entry['payload']:current=entry['payload']['active']
            if current!=row['release']:raise RuntimeError('active candidate changed before round commit')
            expected_previous=self.rows[-1]['release'] if self.rows else None
            if self.release_evidence(row['release'])['payload']['previous']!=expected_previous:raise RuntimeError('candidate ancestry changed before round commit')
            if source_hashes()!=self.cfg['source_hashes']:raise RuntimeError('runtime source changed during iteration')
            if self.cfg['evaluator_command'] and external_evaluation.command_identity(self.cfg['evaluator_command'],PROJECT)!=self.cfg['evaluator_identity']:
                raise RuntimeError('external evaluator changed during iteration')
            _atomic_create(self.run_dir/'rounds'/f"{row['round']:06}.json",canonical({'config_sha256':self.cfg['config_sha256'],'row_sha256':digest(canonical(row)),'row':row})+b'\n')

    def checked_evaluation(self, evaluation, task, rid):
        path=Path(evaluation['receipt_path'])
        if file_digest(path)!=evaluation['receipt_sha256']:raise ValueError('evaluation receipt changed')
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved['status'] not in ('PASS','REJECT'):raise ValueError('evaluation is not a usable result')
        response=external_evaluation.parse_response(canonical(saved['response']),saved['request'])
        release=self.release_evidence(rid)
        if release['task']!=task or response['release_id']!=rid or response['task_sha256']!=release['payload']['task'] or response['wasm_sha256']!=release['payload']['wasm'] or response['evaluator_id']!=self.cfg['evaluator_identity']:
            raise ValueError('evaluation not bound to effective candidate')
        return saved,response

    def pending_repair(self, round_no):
        directory=self.run_dir/'repairs';directory.mkdir(exist_ok=True)
        effective=self.tasks[round_no-1];chain=[]
        for revision,path in enumerate(sorted(directory.glob(f'{round_no:06}-r*.json'))):
            if path.name!=f'{round_no:06}-r{revision:02}.json' or revision>=self.cfg['external_repairs']:raise ValueError('repair sequence or budget mismatch')
            record=json.loads(path.read_text(encoding='utf-8'))
            if record['revision']!=revision or record['config_sha256']!=self.cfg['config_sha256'] or record['effective_task']!=effective:raise ValueError('repair contract chain mismatch')
            saved,response=self.checked_evaluation(record['evaluation'],effective,record['release'])
            if saved['status']!='REJECT' or response['fit_pass'] or not response['regression_pass']:raise ValueError('repair requires fitting rejection and passing regression')
            additions=repair_protocol.validate_feedback(self.tasks[round_no-1],effective,response['feedback'])
            if not additions or additions!=record['feedback']:raise ValueError('repair feedback mismatch')
            next_task=repair_protocol.revision_task(self.tasks[round_no-1],effective,additions,revision+1)
            if next_task!=record['next_task']:raise ValueError('repair revision mismatch')
            chain.append(record);effective=next_task
        return effective,chain,len(chain)

    def load_round_receipts(self):
        receipts=self.run_dir/'rounds';receipts.mkdir(exist_ok=True);rows=[]
        promotions={e['payload']['active'] for e in self.ledger() if e['payload'].get('event')=='PROMOTE'}
        for index,path in enumerate(sorted(receipts.glob('*.json'))):
            if path.name!=f'{index+1:06}.json':raise ValueError('round receipt gap')
            receipt=json.loads(path.read_text(encoding='utf-8'));row=receipt['row']
            if receipt['config_sha256']!=self.cfg['config_sha256'] or receipt['row_sha256']!=digest(canonical(row)):raise ValueError('round receipt checksum mismatch')
            if row['round']!=index+1 or row['release'] not in promotions or row['original_task']!=self.tasks[index]:raise ValueError('receipt not bound to frozen task and promotion')
            effective,chain,_=self.pending_repair(index+1)
            evidence=self.release_evidence(row['release'])
            if row['effective_task']!=effective or row['repair_chain']!=chain or evidence['task']!=effective or row['task_sha256']!=evidence['payload']['task']:raise ValueError('effective task mismatch')
            if evidence['payload']['previous']!=(rows[-1]['release'] if rows else None):raise ValueError('receipt ancestry mismatch')
            if self.cfg['evaluator_command']:
                saved,response=self.checked_evaluation(row['external_evaluation'],effective,row['release'])
                if saved['status']!='PASS' or not response['fit_pass'] or not response['regression_pass']:raise ValueError('completed evaluation not passing')
            rows.append(row)
        if self.rows!=rows[:len(self.rows)]:raise ValueError('checkpoint contradicts round receipts')
        self.rows=rows;return len(rows)

    def run(self,max_new,checkpoint_every,completed=0):
        self.events.mkdir(exist_ok=True);self.checkpoints.mkdir(exist_ok=True)
        if self.call('status',self.state).get('state')=='KILLED':raise ValueError('state STOPped; explicitly resume first')
        self.call('replay',self.state);completed=self.load_round_receipts();self.start_completed=completed
        for idx in range(completed,self.cfg['rounds']):
            if source_hashes()!=self.cfg['source_hashes']:raise RuntimeError('runtime source changed')
            original=self.tasks[idx];effective,chain,revision=self.pending_repair(idx+1)
            expected_previous=self.rows[-1]['release'] if self.rows else None
            rid=None
            try:
                while True:
                    current=self.active()
                    # Repair intent is durable before compensation: recover a crash in that window.
                    if chain and current==chain[-1]['release']:
                        self.rollback_exact(current);current=self.active()
                    if current!=expected_previous:
                        ev=self.release_evidence(current) if current else None
                        if ev is None or ev['task']!=effective or ev['payload']['previous']!=expected_previous:raise ValueError('active state diverged from pending task')
                        rid=current;result={'decision':'PROMOTE','release':rid,'rounds':max(a['round'] for a in ev['payload']['attempts'])}
                    else:
                        path=self.run_dir/'tasks'/f'{idx+1:06}-r{revision:02}.json';_atomic_create(path,canonical(effective)+b'\n')
                        result=self.call('evolve',self.state,path)
                        if result.get('decision')!='PROMOTE':raise RuntimeError('task did not promote')
                        rid=result['release']
                    row=self.verify_release(effective,rid,expected_previous,result)
                    if self.release_evidence(rid)['task']!=effective:raise ValueError('effective task mismatch')
                    evaluation=self.evaluate(effective,rid,idx+1,revision)
                    if evaluation is None or evaluation['status']=='PASS':
                        if evaluation is not None:row['external_evaluation']=evaluation;row['external_feedback']=evaluation['response']['feedback']
                        row.update(original_task=original,effective_task=effective,repair_chain=chain)
                        self.commit_round(row);break
                    response=evaluation.get('response') or {}
                    if evaluation['status']!='REJECT' or response.get('fit_pass') is not False or response.get('regression_pass') is not True or not response.get('feedback') or revision>=self.cfg['external_repairs']:
                        raise RuntimeError(f'external evaluation {evaluation["status"]}; repair unavailable or budget exhausted')
                    additions=repair_protocol.validate_feedback(original,effective,response['feedback'])
                    if not additions:raise ValueError('public feedback adds no new examples')
                    next_task=repair_protocol.revision_task(original,effective,additions,revision+1)
                    record={'config_sha256':self.cfg['config_sha256'],'revision':revision,'release':rid,'effective_task':effective,'next_task':next_task,'feedback':additions,'evaluation':evaluation}
                    _atomic_create(self.run_dir/'repairs'/f'{idx+1:06}-r{revision:02}.json',canonical(record)+b'\n')
                    chain.append(record);effective=next_task;revision+=1
                    self.rollback_exact(rid);rid=None
            except BaseException:
                receipt_path=self.run_dir/'rounds'/f'{idx+1:06}.json'
                if receipt_path.exists():
                    committed=json.loads(receipt_path.read_text(encoding='utf-8'))
                    if committed['row_sha256']!=digest(canonical(committed['row'])) or committed['row']['release']!=rid:raise RuntimeError('ambiguous committed receipt')
                    self.rows.append(committed['row'])
                elif rid is not None and self.active()==rid:self.rollback_exact(rid)
                raise
            self.rows.append(row);completed+=1
            if completed%checkpoint_every==0:self.checkpoint('RUNNING',completed)
            if max_new is not None and completed>=self.start_completed+max_new and completed<self.cfg['rounds']:
                self.checkpoint('PAUSED',completed,{'reason':'max-new-rounds reached'});return
        self.checkpoint('COMPLETE',completed)

def parser():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--rounds",type=int,default=12); p.add_argument("--checkpoint-every",type=int,default=1)
    p.add_argument("--output",type=Path); p.add_argument("--resume",type=Path); p.add_argument("--max-new-rounds",type=int)
    p.add_argument("--proposal-rounds",type=int,default=2); p.add_argument("--timeout",type=int,default=180); p.add_argument("--seed",type=int,default=DEFAULT_SEED)
    p.add_argument("--evaluator-command",help="JSON array command for trusted external evaluator"); p.add_argument("--evaluation-timeout",type=int,default=180)
    p.add_argument("--external-repairs",type=int,default=0,help="bounded public-feedback repair attempts (0..8)")
    p.add_argument("--tasks",type=Path,help="JSON list of task contract objects or task-file paths"); p.add_argument("--plan-only",action="store_true"); return p

def main(argv=None):
    a=parser().parse_args(argv)
    for key in ("rounds","checkpoint_every","proposal_rounds","timeout","evaluation_timeout"):
        if getattr(a,key)<=0:raise SystemExit(f"--{key.replace('_','-')} must be positive")
    if not 0<=a.external_repairs<=8:raise SystemExit("--external-repairs must be 0..8")
    if not 1<=a.proposal_rounds<=8:raise SystemExit("--proposal-rounds must be 1..8")
    if a.max_new_rounds is not None and a.max_new_rounds<=0:raise SystemExit("--max-new-rounds must be positive")
    try:evaluator_command=json.loads(a.evaluator_command) if a.evaluator_command else None
    except json.JSONDecodeError as exc:raise SystemExit(f"--evaluator-command must be a JSON string array: {exc}")
    if evaluator_command is not None and (not isinstance(evaluator_command,list) or not evaluator_command or any(not isinstance(x,str) or not x for x in evaluator_command)):
        raise SystemExit("--evaluator-command must be a non-empty JSON string array")
    if bool(a.output)==bool(a.resume):raise SystemExit("specify exactly one of --output or --resume")
    tasks,oracles=frozen_tasks(task_defs(json.loads(a.tasks.read_text(encoding="utf-8")) if a.tasks else None),a.rounds,a.seed,a.proposal_rounds)
    cfg={"rounds":a.rounds,"checkpoint_every":a.checkpoint_every,"proposal_rounds":a.proposal_rounds,"timeout":a.timeout,"seed":a.seed,
         "external_repairs":a.external_repairs,"tasks_sha256":digest(canonical([json.loads(contract_bytes(t)) for t in tasks])),"task_count":len(tasks),"source_hashes":source_hashes(),
         "task_oracles":oracles,
         "evaluator_command":evaluator_command,"evaluation_timeout":a.evaluation_timeout,
         "evaluator_identity":external_evaluation.command_identity(evaluator_command,PROJECT) if evaluator_command else None}
    if a.plan_only:
        cfg["config_sha256"]=digest(canonical(cfg)); print(json.dumps({"config":cfg,"preview":tasks[:3],"total_tasks":len(tasks)},indent=2,ensure_ascii=False)); return 0
    run_dir=a.resume.resolve() if a.resume else a.output.resolve()
    if a.resume:
        if not run_dir.is_dir():raise SystemExit("resume directory missing")
        config_path=run_dir/'frozen-config.json'
        saved=json.loads((config_path if config_path.exists() else run_dir/'run-intent.json').read_text(encoding='utf-8'))
        if any(saved.get(k)!=v for k,v in cfg.items()):raise SystemExit("resume refused: frozen parameters, tasks, or source/provider/controller hash changed")
    else:run_dir.mkdir(parents=True,exist_ok=False); (run_dir/"tasks").mkdir(); (run_dir/"events").mkdir()
    with recovery.lock(run_dir/".runner.lock"):
        runner=Runner(run_dir,cfg,tasks,a.timeout)
        config_path=run_dir/'frozen-config.json'
        if not config_path.exists():
            _atomic_create(run_dir/'run-intent.json',canonical(cfg)+b'\n')
            (run_dir/'tasks').mkdir(exist_ok=True);(run_dir/'events').mkdir(exist_ok=True)
            if not runner.state.exists():runner.call('init',runner.state)
            else:runner.call('recover',runner.state)
            if len(runner.ledger())!=1 or runner.active() is not None:raise ValueError('unconfigured state is not empty initialization')
            cfg['state_root_sha256']=runner.state_root()
            cfg['config_sha256']=digest(canonical(cfg))
            _atomic_create(config_path,canonical(cfg)+b'\n')
        if a.resume:
            cfg["state_root_sha256"]=runner.state_root(); saved=json.loads((run_dir/"frozen-config.json").read_text(encoding="utf-8"))
            if saved.get("state_root_sha256")!=cfg["state_root_sha256"]:raise SystemExit("resume refused: Rust state root changed")
            cfg["config_sha256"]=saved["config_sha256"]
            if cfg["config_sha256"]!=digest(canonical({k:v for k,v in cfg.items() if k!="config_sha256"})):raise SystemExit("resume refused: frozen config digest mismatch")
            completed,rows=runner.validate_checkpoints(); runner.rows=rows
            if not runner.ledger():raise SystemExit("resume refused: signed Rust ledger missing")
        else:
            completed=0
        try:runner.run(a.max_new_rounds,a.checkpoint_every,completed)
        except BaseException as exc:
            runner.checkpoint("FAIL",len(runner.rows),{"type":type(exc).__name__,"message":str(exc),"traceback":traceback.format_exc()}); raise
    print(json.dumps({"run_directory":str(run_dir),"target_rounds":a.rounds,"completed_rounds":len(runner.rows),"status":runner.last_checkpoint["status"]},indent=2)); return 0
if __name__=="__main__":raise SystemExit(main())
