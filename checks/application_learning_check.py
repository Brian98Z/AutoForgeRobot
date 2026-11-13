#!/usr/bin/env python3
"""Checks source provenance, experience validation and deterministic retrieval."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib,json,sys,tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "autoforge"))
import application_learning as L
import references
def reject(fn):
    try: fn()
    except (L.LearningRefused,ValueError): return
    raise AssertionError("expected refusal")
def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); src=root/"api.py"; src.write_text("def add(a, b): return a + b\n",encoding="utf-8")
        ing=references.ingest(src,root/"ref","local approved source","MIT",kind="source")
        package=references._load_package(root/"ref",ing["package_id"]); sha=package["files"][0]["sha256"]
        source={"package_id":ing["package_id"],"path":"api.py","sha256":sha,"permission":"derive"}
        facts=[{"id":"F1","claim":"add combines two values","provenance":"observation","source":source,"evidence_ids":["source:api.py:line1"]},{"id":"F2","claim":"input validation may be absent","provenance":"unknown","source":source,"evidence_ids":[]}]
        tasks=[{"id":"T1","title":"Implement interface","depends_on":[],"fact_ids":["F1","F2"],"verification_obligations":["verify invalid inputs"]}]
        extracted=L.extract_reference_observations(root/"ref",ing["package_id"])
        assert extracted["files"][0]["interfaces"]==[{"kind":"function","name":"add","line":1,"parameters":["a","b"]}]
        assert extracted["files"][0]["unknowns"][-1]["provenance"]=="unknown"
        assert extracted["license"]=="MIT" and extracted["origin"]=="local approved source"
        model=L.build_reference_model(root/"ref",facts,tasks); assert L.validate_reference_model(model,root/"ref")==model
        bad=json.loads(json.dumps(model)); bad["facts"][0]["claim"]="tampered"; reject(lambda:L.validate_reference_model(bad,root/"ref"))
        import application_spec as spec
        approved=spec.approve_spec(spec.propose_spec({"schema":1,"spec_id":"diagnosis","version":1,"goal":"fix defect","requirements":[{"id":"R1","statement":"Preserve add behavior","provenance":"user","source":"ticket","inputs":["input"],"outputs":["result"],"failures":["invalid input rejected"],"invariants":["existing add preserved"],"state_transitions":[],"non_goals":[],"acceptance_ids":["A1"],"ambiguity_ids":[],"conflict_ids":[]}],"entities":[],"tasks":[{"id":"T1","title":"Verify","depends_on":[],"requirement_ids":["R1"],"acceptance_ids":["A1"],"kind":"verification"}],"oracle":{"public_acceptance_ids":["A1"],"hidden_oracle_ids":[],"separation":"private"},"ambiguities":[],"conflicts":[],"non_goals":[],"approval":{"status":"draft","operator":"","breaking_change_authorized":False,"authorization_source":""}}),operator="op",authorization_source="ticket")
        diagnosis={"path":"src/api.py","line":8,"message":"unexpected output","source_sha256":"a"*64}
        owner={"files":{"src/api.py":{"owner":"api-team","module":"api","sha256":"a"*64,"requirement_ids":["R1"],"acceptance_ids":["A1"]}},"requirement_ids":["R1"],"acceptance_ids":["A1"]}
        mapped=L.map_diagnosis_to_obligations(approved,diagnosis,owner)
        assert mapped["retained_acceptance_ids"]==["A1"] and mapped["diagnosis"]["line"]==8
        stale={**diagnosis,"source_sha256":"b"*64}; reject(lambda:L.map_diagnosis_to_obligations(approved,stale,owner))
        assert mapped["status"]=="MAPPED" and mapped["retained_acceptance_ids"]==["A1"]
        badfacts=json.loads(json.dumps(facts)); badfacts[0]["source"]["sha256"]="0"*64; reject(lambda:L.build_reference_model(root/"ref",badfacts,tasks))
        badfacts=json.loads(json.dumps(facts)); badfacts[0]["provenance"]="model"; reject(lambda:L.build_reference_model(root/"ref",badfacts,tasks))
        base={"schema":1,"experience_id":"repair-1","problem_tags":["validation"],"before_sha256":"1"*64,"after_sha256":"2"*64,"repair_summary":"Reject invalid quantity","environment":{"runtime":"python-3.12","adapter":"isolated-container"},"validator":{"status":"PASS","report_sha256":"3"*64,"validator_id":"independent-v1","independent":True},"invalidation_conditions":["runtime major version changes","validator contract changes"],"source":source}
        report={"status":"PASS","validator_id":"independent-v1","before_sha256":"1"*64,"after_sha256":"2"*64,"environment":{"runtime":"python-3.12","adapter":"isolated-container"}}
        report_path=root/"validator.json"; report_path.write_text(json.dumps(report),encoding="utf-8")
        report_ing=references.ingest(report_path,root/"ref","validator report","internal",kind="source")
        report_sha=references._load_package(root/"ref",report_ing["package_id"])["files"][0]["sha256"]
        report_source={"package_id":report_ing["package_id"],"path":"validator.json","sha256":report_sha,"permission":"inspect"}
        base["validator"].update(report_source=report_source,report_sha256=report_sha)
        record=L.record_experience(base,root/"ref"); L.save_experience(root/"experiences"/"repair-1.json",record,root/"ref")
        result=L.retrieve_experiences(root/"experiences",reference_store=root/"ref",problem_tags=["validation"],environment=base["environment"]); assert len(result)==1 and result[0]["sha256"]==record["sha256"]
        assert L.retrieve_experiences(root/"experiences",reference_store=root/"ref",problem_tags=["validation"],environment={"runtime":"python-3.12"})==[]
        fake={**base,"validator":{**base["validator"],"report_sha256":"3"*64,"validator_id":"model said so"}}; reject(lambda:L.record_experience(fake,root/"ref"))
        badrecord=json.loads((root/"experiences"/"repair-1.json").read_text()); badrecord["repair_summary"]="tamper"; (root/"experiences"/"repair-1.json").write_text(json.dumps(badrecord)); reject(lambda:L.retrieve_experiences(root/"experiences",reference_store=root/"ref",problem_tags=["validation"],environment=base["environment"]))
    print("application_learning_check: PASS (verified refs, provenance, validator-bound immutable experience, deterministic environment retrieval, tamper refusal)")
if __name__=="__main__": main()
