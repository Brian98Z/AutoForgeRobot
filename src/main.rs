mod persistence;
mod sandbox;

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use fs2::FileExt;
use rand::rngs::OsRng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{env, fs::{self, File, OpenOptions}, io::{Read, Write}, path::{Path, PathBuf}, process::{Command, Stdio}, sync::mpsc, thread, time::{Duration, Instant}};

type Result<T, E = String> = std::result::Result<T, E>;
const CHARTER: &str = "autoforge/v0: offline-only; candidates=wasm-no-imports; ABI=solve(i64,i64)->i64; fuel=100000; both-lines-pass; unknown=HOLD; host-providers-trusted; operator-kill; no-self-modification";
const FUEL: u64 = 100_000;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Case { input: [i64; 2], expected: i64 }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reference { id:String, content_sha256:String, content:String, kind:String, origin:String, license:String }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Task { schema: u32, id: String, goal: String, public_cases: Vec<Case>, holdout_cases: Vec<Case>, max_rounds: u32, #[serde(default,skip_serializing_if="Vec::is_empty")] references:Vec<Reference> }
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Proposal { wat: String, strategy: String }
#[derive(Serialize, Deserialize)]
struct Envelope { payload: Value, signature: String }

fn hash(bytes: &[u8]) -> String { hex::encode(Sha256::digest(bytes)) }
fn bytes(v: &impl Serialize) -> Result<Vec<u8>> { serde_json::to_vec(v).map_err(|e| e.to_string()) }
fn read(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let mut b = Vec::new(); File::open(path).map_err(|e| format!("{}: {e}", path.display()))?.take(limit + 1).read_to_end(&mut b).map_err(|e| e.to_string())?;
    if b.len() as u64 > limit { return Err("size budget exceeded".into()); } Ok(b)
}
fn load<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> { serde_json::from_slice(&read(path, 4_000_000)?).map_err(|e| e.to_string()) }
fn new_file(path: &Path, data: &[u8]) -> Result<()> { persistence::publish_file(path, data) }
fn key_from_private(root:&Path)->Result<SigningKey> {
    let b=read(&root.join("private.key"),32)?;let raw:[u8;32]=b.try_into().map_err(|_|"invalid key")?;Ok(SigningKey::from_bytes(&raw))
}
fn stopped(root: &Path) -> Result<()> { if root.join("STOP").exists() { Err("KILL: operator stop latched; use resume explicitly".into()) } else { Ok(()) } }
fn key(root: &Path) -> Result<SigningKey> {
    let k = key_from_private(root)?;
    if read(&root.join("public.key"), 32)? != k.verifying_key().as_bytes() { return Err("key mismatch".into()); } Ok(k)
}
fn verify(root: &Path, envelope: &Envelope) -> Result<()> {
    let raw: [u8;32] = read(&root.join("public.key"), 32)?.try_into().map_err(|_| "invalid public key")?;
    let vk = VerifyingKey::from_bytes(&raw).map_err(|e| e.to_string())?;
    let sig = Signature::from_slice(&hex::decode(&envelope.signature).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    vk.verify(&bytes(&envelope.payload)?, &sig).map_err(|_| "evidence signature mismatch".to_owned())
}
fn signed(root: &Path, payload: Value) -> Result<Envelope> { let signature = hex::encode(key(root)?.sign(&bytes(&payload)?).to_bytes()); Ok(Envelope {payload, signature}) }
fn put_blob(root: &Path, data: &[u8]) -> Result<String> {
    let id = hash(data); let p = root.join("blobs").join(&id);
    if p.exists() { if read(&p, 4_000_000)? != data { return Err("blob hash collision or tampering".into()); } }
    else { match persistence::publish_file(&p, data) { Ok(()) => {}, Err(_) if p.exists() => { if read(&p, 4_000_000)? != data { return Err("blob hash collision or tampering".into()); } }, Err(e) => return Err(e) } }
    Ok(id)
}
fn blob(root: &Path, id: &str) -> Result<Vec<u8>> {
    if id.len()!=64 || !id.bytes().all(|c| c.is_ascii_hexdigit()) { return Err("invalid content address".into()); }
    let b = read(&root.join("blobs").join(id), 4_000_000)?; if hash(&b)!=id { return Err("artifact integrity failure".into()); } Ok(b)
}
fn text<'a>(v: &'a Value, field: &str) -> Result<&'a str> { v[field].as_str().ok_or_else(|| format!("missing {field}")) }
fn ledger(root: &Path) -> Result<Vec<Envelope>> {
    let mut paths: Vec<PathBuf> = fs::read_dir(root.join("ledger")).map_err(|e|e.to_string())?.map(|e|e.map(|e|e.path()).map_err(|e|e.to_string())).collect::<Result<_>>()?;
    paths.retain(|p|p.extension().is_some_and(|e|e=="json")); paths.sort();
    let mut entries = Vec::new(); let mut prior = String::new();
    for (n,p) in paths.iter().enumerate() {
        if p.file_name().unwrap().to_string_lossy()!=format!("{n:08}.json") { return Err("ledger gap".into()); }
        let e: Envelope = load(p)?; verify(root,&e)?;
        if text(&e.payload,"parent")? != prior || e.payload["sequence"] != n as u64 { return Err("ledger chain mismatch".into()); }
        prior=hash(&bytes(&e)?); entries.push(e);
    } Ok(entries)
}

fn commit_lock(root:&Path)->Result<File> {
    let f=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(root.join("commit.lock")).map_err(|e|e.to_string())?;
    FileExt::lock_exclusive(&f).map_err(|e|e.to_string())?; Ok(f)
}
fn append(root:&Path,payload:Value)->Result<()> { append_with_stop_removal(root,payload,|path|fs::remove_file(path)) }
fn append_with_stop_removal(root: &Path, mut payload: Value, remove:impl FnOnce(&Path)->std::io::Result<()>) -> Result<()> {
    let _commit=commit_lock(root)?;
    let entries=ledger(root)?; payload["sequence"]=json!(entries.len()); payload["parent"]=json!(entries.last().map(|e|bytes(e).map(|b|hash(&b))).transpose()?.unwrap_or_default());
    let event=payload["event"].as_str().unwrap_or("").to_owned();
    if event=="PROMOTE" || event=="ROLLBACK" { stopped(root)?; }
    let e=signed(root,payload)?; let name=format!("{:08}.json",entries.len());
    persistence::publish_file(&root.join("ledger").join(name), &bytes(&e)?)?;
    if event=="OPERATOR_RESUME" { clear_stop_after_authorization(root, remove)?; }
    Ok(())
}
fn clear_stop_after_authorization(root:&Path, remove:impl FnOnce(&Path)->std::io::Result<()>) -> Result<()> {
    let stop=root.join("STOP"); if !stop.exists(){return Ok(());}
    remove(&stop).map_err(|e|format!("resume authorization committed; STOP remains latched, retry `resume` explicitly: {e}"))?;
    if let Err(e)=persistence::sync_parent(&stop){eprintln!("warning: resume authorization committed and STOP removal requested, but directory sync failed: {e}");}
    Ok(())
}
fn active(entries: &[Envelope]) -> Option<String> { entries.iter().rev().find(|e| e.payload.get("active").is_some()).and_then(|e|e.payload["active"].as_str().map(str::to_owned)) }
fn release(root: &Path, id: &str) -> Result<Value> {
    let e: Envelope=serde_json::from_slice(&blob(root,id)?).map_err(|e|e.to_string())?; verify(root,&e)?;
    for field in ["wasm","wat","task","audit_wasm"] { blob(root,text(&e.payload,field)?)?; } Ok(e.payload)
}
fn check(root: &Path, wasm: &[u8], cases: &[Case]) -> Result<Vec<Value>> {
    let mut failures=Vec::new();
    for c in cases { stopped(root)?; let r=sandbox::evaluate(wasm,c.input,FUEL); if r.as_ref().ok()!=Some(&c.expected) { failures.push(json!({"input":c.input,"expected":c.expected,"actual":r.as_ref().ok(),"error":r.err()})); } } Ok(failures)
}
fn check_release(root: &Path, id: &str) -> Result<()> {
    let r=release(root,id)?; let task:Task=serde_json::from_slice(&blob(root,text(&r,"task")?)?).map_err(|e|e.to_string())?;
    let source=String::from_utf8(blob(root,text(&r,"wat")?)?).map_err(|e|e.to_string())?;
    if hash(&sandbox::compile(&source)?)!=text(&r,"wasm")? { return Err("source/build hash mismatch".into()); }
    if let Some(attempts)=r["attempts"].as_array() { for attempt in attempts { if let Some(wat)=attempt["wat"].as_str() { let source=String::from_utf8(blob(root,wat)?).map_err(|e|e.to_string())?; let wasm=text(attempt,"wasm")?; blob(root,wasm)?; if hash(&sandbox::compile(&source)?)!=wasm{return Err("attempt provenance mismatch".into());} } } }
    let cases:Vec<Case>=task.public_cases.into_iter().chain(task.holdout_cases).collect();
    for f in ["wasm","audit_wasm"] { if !check(root,&blob(root,text(&r,f)?)?,&cases)?.is_empty() { return Err("release replay failed".into()); } } Ok(())
}
fn provider(root: &Path, project: &Path, line: &str, request: &Value) -> Result<Proposal> {
    stopped(root)?;
    let mut cmd=if line=="python" { let mut c=Command::new(env::var("AUTOFORGE_PYTHON").unwrap_or("python".into())); c.arg(project.join("providers/python_agent.py")); c } else {
        let portable=project.join("runtime/julia-1.13.3/bin/julia.exe");
        let exe=env::var("AUTOFORGE_JULIA").unwrap_or_else(|_| if portable.exists(){portable.to_string_lossy().into_owned()}else{"julia".into()});
        let mut c=Command::new(exe); c.arg("--startup-file=no").arg(format!("--project={}",project.join("providers").display())).arg(project.join("providers/julia_agent.jl")); c
    };
    // Fixed, operator-owned providers are trusted. Only their returned WASM is untrusted.
    cmd.current_dir(project).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child=cmd.spawn().map_err(|e|format!("{line} provider unavailable: {e}"))?;
    let mut input=child.stdin.take().unwrap(); let data=bytes(request)?;
    let writer=thread::spawn(move||input.write_all(&data));
    let stdout=child.stdout.take().unwrap(); let (tx,rx)=mpsc::channel();
    thread::spawn(move||{let mut b=Vec::new(); let result=stdout.take(131073).read_to_end(&mut b).map(|_|b); let _=tx.send(result);});
    let start=Instant::now(); let mut captured=None;
    loop {
        if stopped(root).is_err() || start.elapsed()>Duration::from_secs(90) { let _=child.kill(); let _=child.wait(); return Err("provider killed or 90-second budget exhausted".into()); }
        if let Some(status)=child.try_wait().map_err(|e|e.to_string())? {
            writer.join().map_err(|_|"provider input failure")?.map_err(|e|e.to_string())?;
            let out=match captured.take() { Some(out)=>out, None=>rx.recv_timeout(Duration::from_secs(2)).map_err(|_|"provider output deadline")?.map_err(|e|e.to_string())? };
            if !status.success() { return Err(format!("{line} proposal failed (exit {status}); unsupported goal or provider configuration")); }
            if out.len()>131072 { return Err("provider output budget exceeded".into()); }
            return serde_json::from_slice(&out).map_err(|e|format!("invalid {line} proposal: {e}"));
        }
        if captured.is_none() { if let Ok(result)=rx.try_recv() { let out=result.map_err(|e|e.to_string())?; if out.len()>131072 {let _=child.kill();let _=child.wait();return Err("provider output budget exceeded".into());} captured=Some(out); } }
        thread::sleep(Duration::from_millis(25));
    }
}
fn validate_task(t:&Task)->Result<()> {
    if t.schema!=1 || t.id.is_empty() || t.id.len()>128 || t.goal.is_empty() || t.goal.len()>16000 || t.public_cases.is_empty() || t.holdout_cases.is_empty() || t.public_cases.len()+t.holdout_cases.len()>256 || !(1..=8).contains(&t.max_rounds) { return Err("InvalidContract: schema/goal/cases/round limits".into()); }
    if t.references.len()>4{return Err("InvalidContract: at most four references".into());}
    for r in &t.references {if r.id.is_empty()||r.id.len()>128||r.content.len()>32768||r.origin.len()>2048||r.license.is_empty()||r.license.len()>512||!matches!(r.kind.as_str(),"wat"|"source"|"rea-evidence")||hash(r.content.as_bytes())!=r.content_sha256{return Err("InvalidContract: invalid reference provenance/content".into());}}
    let mut seen=std::collections::BTreeMap::new(); for c in t.public_cases.iter().chain(&t.holdout_cases) { if seen.insert(c.input,c.expected).is_some_and(|v|v!=c.expected) {return Err("Ambiguous: contradictory acceptance examples".into());} } Ok(())
}
fn evolve(root:&Path,project:&Path,task_path:&Path)->Result<Value> {
    stopped(root)?; let task:Task=load(task_path)?; validate_task(&task)?;
    let before=active(&ledger(root)?); let baseline=before.as_ref().map(|id|release(root,id)).transpose()?;
    let baseline_wat=baseline.as_ref().map(|r|blob(root,text(r,"wat")?).and_then(|b|String::from_utf8(b).map_err(|e|e.to_string()))).transpose()?;
    let mut anchors=Vec::new();
    let mut prior_tasks=std::collections::BTreeSet::new();
    for event in ledger(root)? { if event.payload["event"]=="PROMOTE" { let r=release(root,text(&event.payload,"active")?)?; let task_hash=text(&r,"task")?; if prior_tasks.insert(task_hash.to_owned()) { let old:Task=serde_json::from_slice(&blob(root,task_hash)?).map_err(|e|e.to_string())?; if old.id==task.id {anchors.extend(old.public_cases);anchors.extend(old.holdout_cases);} } } }
    if anchors.len()>4096 { return Err("historical regression budget exceeded; HOLD".into()); }
    let task_id=put_blob(root,&bytes(&task)?)?;
    let manifest=json!({"charter":hash(CHARTER.as_bytes()),"task":task_id,"control_binary":hash(&read(&env::current_exe().map_err(|e|e.to_string())?,100_000_000)?),"cargo_lock":hash(&read(&project.join("Cargo.lock"),1_000_000)?),"python_provider":hash(&read(&project.join("providers/python_agent.py"),1_000_000)?),"julia_provider":hash(&read(&project.join("providers/julia_agent.jl"),1_000_000)?),"julia_protocol":hash(&read(&project.join("providers/protocol.jl"),1_000_000)?),"julia_dependencies":hash(&read(&project.join("providers/Manifest.toml"),1_000_000)?),"model":env::var("AUTOFORGE_MODEL").unwrap_or_else(|_|"offline-synthesis".into()),"baseline":before,"fuel_per_case":FUEL,"provider_timeout_seconds":90,"mode":"offline-local-promotion"});
    let mut attempts=Vec::new(); let mut feedback=[Vec::<Value>::new(),Vec::<Value>::new()];
    let mut revisions=[baseline_wat.clone(),baseline_wat];
    let mut rejected_wats:[Vec<String>;2]=[Vec::new(),Vec::new()];
    let mut accepted_line=[false,false];
    for round in 1..=task.max_rounds {
        stopped(root)?; let mut candidates=Vec::new();
        for (index,line) in ["python","julia"].iter().enumerate() {
            let request=json!({"schema":1,"task":{"id":task.id,"goal":task.goal,"public_cases":task.public_cases,"references":task.references},"round":if accepted_line[index]{1}else{round},"baseline_wat":revisions[index],"feedback":feedback[index],"rejected_wats":rejected_wats[index]});
            eprintln!("round {round}/{}: {line} propose -> independent verification",task.max_rounds);
            let start=Instant::now();
            let result=provider(root,project,line,&request).and_then(|p|sandbox::compile(&p.wat).map(|b|(p,b)));
            match result {
                Err(e)=>{ attempts.push(json!({"round":round,"line":line,"decision":"REJECT","error":e})); feedback[index]=vec![json!({"error":e})]; },
                Ok((p,wasm))=>{
                    revisions[index]=Some(p.wat.clone());
                    let public=check(root,&wasm,&task.public_cases)?; let hidden=check(root,&wasm,&task.holdout_cases)?; let regressions=check(root,&wasm,&anchors)?;
                    accepted_line[index]=public.is_empty()&&hidden.is_empty()&&regressions.is_empty();
                    if !accepted_line[index] && !rejected_wats[index].contains(&p.wat){rejected_wats[index].push(p.wat.clone());}
                    let ok=public.is_empty()&&hidden.is_empty()&&regressions.is_empty(); feedback[index]=public.clone();
                    let wasm_id=put_blob(root,&wasm)?;let wat_id=put_blob(root,p.wat.as_bytes())?;
                    attempts.push(json!({"round":round,"line":line,"strategy":p.strategy,"wasm":wasm_id,"wat":wat_id,"public_failures":public,"holdout_failure_count":hidden.len(),"regression_failure_count":regressions.len(),"decision":if ok{"PASS"}else{"REJECT"},"elapsed_ms":start.elapsed().as_millis()}));
                    if ok { candidates.push((wasm_id,wat_id,wasm)); }
                }
            }
        }
        if candidates.len()==2 {
            // Same frozen inputs, independent exact oracle; agreement is not a proof.
            let cases:Vec<Case>=task.public_cases.iter().chain(&task.holdout_cases).cloned().collect();
            for (_,_,wasm) in &candidates { if !check(root,wasm,&cases)?.is_empty() {return Err("shadow replay failed".into());} }
            let bad=format!("(module (func (export \"solve\") (param i64 i64) (result i64) i64.const {}))",cases[0].expected.wrapping_add(1));
            if check(root,&sandbox::compile(&bad)?,&cases)?.is_empty(){return Err("mutation sentinel escaped oracle".into());}
            if let Some(id)=&before {check_release(root,id)?;}
            let r=signed(root,json!({"schema":1,"wasm":candidates[0].0,"wat":candidates[0].1,"audit_wasm":candidates[1].0,"task":task_id,"previous":before,"manifest":manifest,"attempts":attempts,"gates":{"oracle":true,"dual_line":true,"shadow_replay":true,"mutation_detected":true,"previous_replayed":before.is_some()},"scope":"local offline artifact only; no production release"}))?;
            let id=put_blob(root,&bytes(&r)?)?;
            check_release(root,&id)?;
            // Exercise the same signed journal and reader in an isolated local routing namespace.
            let drill=root.join("drills").join(format!("{:016x}",rand::random::<u64>()));
            fs::create_dir_all(drill.join("ledger")).map_err(|e|e.to_string())?;
            for file in ["private.key","public.key"] { new_file(&drill.join(file),&read(&root.join(file),32)?)?; }
            append(&drill,json!({"event":"PROMOTE","active":id}))?;
            if active(&ledger(&drill)?).as_deref()!=Some(id.as_str()){return Err("routing promotion drill failed".into());}
            append(&drill,json!({"event":"ROLLBACK","active":before}))?;
            let restored=ledger(&drill)?;
            if restored.last().unwrap().payload["active"]!=json!(before){return Err("routing rollback drill failed".into());}
            let drill_hash=put_blob(root,&bytes(&restored)?)?;
            fs::remove_dir_all(&drill).map_err(|e|e.to_string())?;
            stopped(root)?; append(root,json!({"event":"PROMOTE","active":id,"previous":before,"manifest":manifest,"rollback_drill":drill_hash}))?;
            return Ok(json!({"decision":"PROMOTE","release":id,"rounds":round,"scope":"offline-local","previous":before}));
        }
    }
    append(root,json!({"event":"HOLD","manifest":manifest,"attempts":attempts,"reason":"both lines must pass all gates in same round"}))?;
    Ok(json!({"decision":"HOLD","active":before,"reason":"budget exhausted; active artifact unchanged"}))
}
fn init_recovery_lock(root:&Path)->Result<File> {
    let parent=root.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));fs::create_dir_all(parent).map_err(|e|e.to_string())?;
    let name=root.file_name().ok_or("state path has no filename")?.to_string_lossy();
    let lock=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(parent.join(format!(".{name}.autoforge-init.lock"))).map_err(|e|e.to_string())?;
    FileExt::lock_exclusive(&lock).map_err(|e|format!("init/recovery busy: {e}"))?;Ok(lock)
}
fn init(root:&Path)->Result<Value> {
    let _init_lock=init_recovery_lock(root)?;
    let parent=root.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new(".")); fs::create_dir_all(parent).map_err(|e|e.to_string())?;
    let stage=parent.join(format!(".autoforge-init-{}",rand::random::<u64>())); fs::create_dir(&stage).map_err(|e|e.to_string())?;
    let result=(|| {
        fs::create_dir(stage.join("blobs")).and_then(|_|fs::create_dir(stage.join("ledger"))).map_err(|e|e.to_string())?;
        let k=SigningKey::generate(&mut OsRng);new_file(&stage.join("private.key"),&k.to_bytes())?;new_file(&stage.join("public.key"),k.verifying_key().as_bytes())?;new_file(&stage.join("charter.txt"),CHARTER.as_bytes())?;
        append(&stage,json!({"event":"INIT","charter":hash(CHARTER.as_bytes())}))?; persistence::sync_parent(&stage.join("ledger"))?; persistence::sync_parent(&stage.join("private.key"))?; persistence::publish_dir_noreplace(&stage,root)?;
        Ok(json!({"initialized":root,"public_key":hex::encode(k.verifying_key().as_bytes()),"security":"operator-account local prototype; not OS-separated signing"}))
    })(); if stage.exists(){let _=fs::remove_dir_all(&stage);} result
}
fn recover_unreferenced_blobs(root:&Path)->Result<Vec<String>> {
    let entries=ledger(root)?;
    let mut referenced=std::collections::BTreeSet::new();
    fn collect(v:&Value,out:&mut Vec<String>){match v {Value::Object(o)=>for (k,x) in o {if matches!(k.as_str(),"active"|"previous"|"wasm"|"wat"|"task"|"audit_wasm"|"rollback_drill") {if let Some(s)=x.as_str(){if s.len()==64&&s.bytes().all(|b|b.is_ascii_hexdigit()){out.push(s.to_owned());}}} collect(x,out)},Value::Array(a)=>for x in a{collect(x,out)},_=>{}}}
    fn visit(root:&Path,id:&str,seen:&mut std::collections::BTreeSet<String>)->Result<()> {
        if id.len()!=64||!id.bytes().all(|b|b.is_ascii_hexdigit()){return Err("invalid referenced content address during recovery".into());}
        if !seen.insert(id.to_owned()){return Ok(());}
        let data=blob(root,id)?;
        if let Ok(env)=serde_json::from_slice::<Envelope>(&data){verify(root,&env)?;let mut children=Vec::new();collect(&env.payload,&mut children);for child in children{if child!=id{visit(root,&child,seen)?;}}}
        Ok(())
    }
    for e in &entries {let mut ids=Vec::new();collect(&e.payload,&mut ids);for id in ids{visit(root,&id,&mut referenced)?;}}
    let dir=root.join("blobs");let quarantine=root.join("quarantine");let mut moved=Vec::new();if !dir.exists(){return Ok(moved)}
    for item in fs::read_dir(&dir).map_err(|e|e.to_string())? {let p=item.map_err(|e|e.to_string())?.path();let Some(id)=p.file_name().and_then(|n|n.to_str()) else {continue};if id.len()!=64||!id.bytes().all(|b|b.is_ascii_hexdigit())||referenced.contains(id){continue;}
        let data=match read(&p,4_000_000){Ok(b)=>b,Err(_)=>continue};if hash(&data)==id{continue;}
        fs::create_dir_all(&quarantine).map_err(|e|e.to_string())?;let dest=quarantine.join(format!("{id}.{}.damaged",rand::random::<u64>()));
        match fs::rename(&p,&dest){Ok(())=>{if let Err(e)=persistence::sync_parent(&p){eprintln!("warning: quarantined {id} but directory sync failed: {e}");}moved.push(id.to_owned());},Err(e)=>return Err(format!("cannot quarantine damaged unreferenced blob {id}: {e}"))}
    }
    Ok(moved)
}
fn recover_init(root:&Path)->Result<Value> {
    if !root.exists(){fs::create_dir_all(root).map_err(|e|e.to_string())?;}
    if root.join("ledger/00000000.json").exists(){let entries=ledger(root)?;if entries.first().is_some_and(|e|e.payload["event"]=="INIT"){return Ok(json!({"recovered":false,"initialized":true,"ledger_entries":entries.len()}));}}
    if root.join("ledger").exists() && fs::read_dir(root.join("ledger")).map_err(|e|e.to_string())?.next().is_some(){return Err("refusing recovery: non-INIT ledger data exists".into());}
    if root.join("charter.txt").exists() && read(&root.join("charter.txt"),4096)?!=CHARTER.as_bytes(){return Err("refusing recovery: charter mismatch".into());}
    if root.join("private.key").exists() && read(&root.join("private.key"),32)?.len()!=32{return Err("refusing recovery: invalid private key; preserve state".into());}
    if root.join("private.key").exists() && root.join("public.key").exists(){key(root)?;} else if root.join("private.key").exists(){let k=key_from_private(root)?;persistence::publish_file(&root.join("public.key"),k.verifying_key().as_bytes())?;} else if root.join("public.key").exists(){return Err("public-only partial state cannot recover signing identity; preserve it and initialize a new state explicitly".into());} else {let k=SigningKey::generate(&mut OsRng);new_file(&root.join("private.key"),&k.to_bytes())?;new_file(&root.join("public.key"),k.verifying_key().as_bytes())?;}
    fs::create_dir_all(root.join("blobs")).map_err(|e|e.to_string())?;fs::create_dir_all(root.join("ledger")).map_err(|e|e.to_string())?;
    if !root.join("charter.txt").exists(){new_file(&root.join("charter.txt"),CHARTER.as_bytes())?;}
    if fs::read_dir(root.join("ledger")).map_err(|e|e.to_string())?.next().is_none(){append(root,json!({"event":"INIT","charter":hash(CHARTER.as_bytes())}))?;}
    let entries=ledger(root)?;if entries.first().map_or(true,|e|e.payload["event"]!="INIT"){return Err("recovery did not produce valid INIT ledger".into());}
    Ok(json!({"recovered":true,"initialized":true,"ledger_entries":entries.len(),"limits":"only empty/partial INIT repaired; preserves keys; does not replace blobs"}))
}
fn recover(root:&Path)->Result<Value> {
    let _init=init_recovery_lock(root)?;
    if !root.exists(){fs::create_dir(root).map_err(|e|e.to_string())?;}
    if !root.join("ledger/00000000.json").exists() {
        for item in fs::read_dir(root).map_err(|e|e.to_string())? {
            let item=item.map_err(|e|e.to_string())?;
            let name=item.file_name();let name=name.to_string_lossy();
            if !matches!(name.as_ref(),"blobs"|"ledger"|"private.key"|"public.key"|"charter.txt"|"STOP"|"control.lock"|"commit.lock") {
                return Err(format!("refusing recovery: unrelated entry {name}"));
            }
            if item.file_type().map_err(|e|e.to_string())?.is_symlink(){return Err("refusing recovery through links".into());}
        }
    }
    let control=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(root.join("control.lock")).map_err(|e|e.to_string())?;
    control.try_lock_exclusive().map_err(|_|"controller busy; recovery not started")?;
    let initialized=recover_init(root)?;
    let _commit=commit_lock(root)?;
    let quarantined=recover_unreferenced_blobs(root)?;
    Ok(json!({"init":initialized,"quarantined_unreferenced_damaged_blobs":quarantined,"limits":"referenced corruption is never altered; only hash-invalid unreferenced blobs are quarantined"}))
}
fn run()->Result<Value> {
    let args:Vec<String>=env::args().collect(); let command=args.get(1).map(String::as_str).unwrap_or("help");
    if command=="help" {return Ok(json!({"usage":["autoforge init STATE","autoforge recover STATE","autoforge evolve STATE TASK.json","autoforge execute STATE X Y","autoforge status STATE","autoforge replay STATE","autoforge rollback STATE","autoforge kill STATE","autoforge resume STATE"],"project":"AUTOFORGE_PROJECT defaults to current directory","mode":"v0 offline local prototype"}));}
    let root=PathBuf::from(args.get(2).ok_or("missing state directory")?);
    if command=="recover" { return recover(&root); }
    if command=="init" {return init(&root);}
    if command=="kill" { let _commit=commit_lock(&root)?; if !root.join("STOP").exists(){persistence::publish_file(&root.join("STOP"),b"operator kill")?;}return Ok(json!({"state":"KILLED"})); }
    if read(&root.join("charter.txt"),4096)?!=CHARTER.as_bytes(){return Err("KILL: immutable charter mismatch".into());}
    let lock=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(root.join("control.lock")).map_err(|e|e.to_string())?;
    lock.try_lock_exclusive().map_err(|_|"controller busy; kill remains available")?;
    let entries=ledger(&root)?;
    match command {
        "resume"=>{append(&root,json!({"event":"OPERATOR_RESUME"}))?;Ok(json!({"state":"READY","authorization":"OPERATOR_RESUME committed"})) },
        "status"=>Ok(json!({"state":if root.join("STOP").exists(){"KILLED"}else{"READY"},"active":active(&entries),"ledger_entries":entries.len()})),
        "evolve"=>{let project=env::var("AUTOFORGE_PROJECT").map(PathBuf::from).unwrap_or(env::current_dir().map_err(|e|e.to_string())?);evolve(&root,&project,Path::new(args.get(3).ok_or("missing task")?))},
        "execute"=>{stopped(&root)?;let id=active(&entries).ok_or("no active release")?;let r=release(&root,&id)?;let x=args.get(3).ok_or("missing x")?.parse().map_err(|_|"invalid i64 x")?;let y=args.get(4).ok_or("missing y")?.parse().map_err(|_|"invalid i64 y")?;let value=sandbox::evaluate(&blob(&root,text(&r,"wasm")?)?,[x,y],FUEL)?;stopped(&root)?;Ok(json!({"release":id,"input":[x,y],"output":value}))},
        "replay"=>{stopped(&root)?;let mut count=0;for e in &entries {if e.payload["event"]=="PROMOTE"{check_release(&root,text(&e.payload,"active")?)?;let drill:Vec<Envelope>=serde_json::from_slice(&blob(&root,text(&e.payload,"rollback_drill")?)?).map_err(|e|e.to_string())?;let mut parent=String::new();for entry in drill {verify(&root,&entry)?;if text(&entry.payload,"parent")?!=parent{return Err("drill evidence chain mismatch".into());}parent=hash(&bytes(&entry)?);}count+=1;}}Ok(json!({"verified_releases":count,"ledger_signatures":"verified"}))},
        "rollback"=>{stopped(&root)?;let current=active(&entries);if let Some(expected)=args.get(3){if current.as_deref()!=Some(expected.as_str()){return Err("rollback conflict: active release differs from expected".into());}}let Some(current)=current else { return Ok(json!({"decision":"ROLLBACK","active":Value::Null,"changed":false})); };let r=release(&root,&current)?;let previous=r["previous"].as_str().map(str::to_owned);if let Some(id)=&previous{check_release(&root,id)?;}stopped(&root)?;append(&root,json!({"event":"ROLLBACK","active":previous,"from":current}))?;Ok(json!({"decision":"ROLLBACK","active":previous}))},
        _=>Err("unknown command; use help".into())
    }
}

#[cfg(test)]
mod transaction_tests {
    use super::*;
    fn root(label:&str)->PathBuf { let p=env::temp_dir().join(format!("autoforge-{label}-{}",rand::random::<u64>()));fs::create_dir_all(&p).unwrap();p }
    #[test] fn failed_noreplace_keeps_existing_bytes() { let p=root("publish");let dst=p.join("evidence");fs::write(&dst,b"committed").unwrap();assert!(persistence::publish_file(&dst,b"replacement").is_err());assert_eq!(fs::read(&dst).unwrap(),b"committed");let _=fs::remove_dir_all(p); }
    #[test] fn orphan_temp_is_ignored_by_ledger() { let p=root("orphan");fs::create_dir_all(p.join("ledger")).unwrap();fs::write(p.join("ledger/00000000.99.tmp"),b"partial").unwrap();let k=SigningKey::generate(&mut OsRng);new_file(&p.join("private.key"),&k.to_bytes()).unwrap();new_file(&p.join("public.key"),k.verifying_key().as_bytes()).unwrap();assert!(ledger(&p).unwrap().is_empty());let _=fs::remove_dir_all(p); }
    #[test] fn interrupted_init_recovery_preserves_existing_key() { let p=root("init-recovery");fs::create_dir_all(p.join("blobs")).unwrap();fs::create_dir_all(p.join("ledger")).unwrap();let k=SigningKey::generate(&mut OsRng);new_file(&p.join("private.key"),&k.to_bytes()).unwrap();new_file(&p.join("public.key"),k.verifying_key().as_bytes()).unwrap();let before=read(&p.join("private.key"),32).unwrap();recover_init(&p).unwrap();assert_eq!(read(&p.join("private.key"),32).unwrap(),before);assert_eq!(ledger(&p).unwrap()[0].payload["event"],"INIT");let _=fs::remove_dir_all(p); }
    #[test] fn failed_resume_removal_keeps_stop_after_committed_authorization() { let p=root("resume-fail");fs::create_dir_all(p.join("blobs")).unwrap();fs::create_dir_all(p.join("ledger")).unwrap();let k=SigningKey::generate(&mut OsRng);new_file(&p.join("private.key"),&k.to_bytes()).unwrap();new_file(&p.join("public.key"),k.verifying_key().as_bytes()).unwrap();append(&p,json!({"event":"INIT","charter":hash(CHARTER.as_bytes())})).unwrap();new_file(&p.join("STOP"),b"operator kill").unwrap();let result=append_with_stop_removal(&p,json!({"event":"OPERATOR_RESUME"}),|_|Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied,"injected removal failure")));assert!(result.unwrap_err().contains("authorization committed"));assert!(p.join("STOP").exists());let entries=ledger(&p).unwrap();assert_eq!(entries.last().unwrap().payload["event"],"OPERATOR_RESUME");let _=fs::remove_dir_all(p); }
}
fn main(){ match run(){Ok(v)=>{println!("{}",serde_json::to_string_pretty(&v).unwrap());if v["decision"]=="HOLD"{std::process::exit(2);}},Err(e)=>{eprintln!("{}",json!({"error":e}));std::process::exit(1);}} }
