#!/usr/bin/env python3
"""Consumer-visible snapshot, dependency, tamper and overwrite boundaries."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import tempfile
from pathlib import Path
import application_evaluation as evaluation
import application_stage_evidence as stages
def refused(fn):
    try:
        fn()
    except (ValueError, OSError):
        return
    raise AssertionError("expected refusal")
def main():
    with tempfile.TemporaryDirectory(prefix="autoforge-stage-") as tmp:
        root = Path(tmp)
        source = root / "source.json"
        source.write_text('{"status":"FAIL"}\n', encoding="utf-8")
        plan = {"schema": 1, "scope": "boundary check", "stages": {
            gate: {"scope": "fixture declaration, not actual gate acceptance", "evidence": {
                role: ["source.json"] for role in definition["evidence"]}, "blockers": ["fixture is not PASS"]}
            for gate, definition in evaluation.GATES.items()}}
        bundle = root / "bundle"
        result = stages.capture(plan, root, bundle)
        assert result["status"] == "READY_FOR_REVIEW" and result["approval"] is None
        anchor = stages.digest((bundle / "index.json").read_bytes())
        refused(lambda: stages.verify(bundle, expected_index_sha256="0" * 64))
        stages.verify(bundle, expected_index_sha256=anchor)
        original = (bundle / "G0/preflight_evidence-0.bin").read_bytes()
        source.write_text('{"status":"PASS"}\n', encoding="utf-8")
        stages.verify(bundle, expected_index_sha256=anchor)
        assert (bundle / "G0/preflight_evidence-0.bin").read_bytes() == original
        refused(lambda: stages.capture(plan, root, bundle))
        snapshot = bundle / "G4/deployment_evidence-0.bin"
        snapshot.write_bytes(snapshot.read_bytes() + b" ")
        refused(lambda: stages.verify(bundle, expected_index_sha256=anchor))
        snapshot.write_bytes(original)
        stages.verify(bundle, expected_index_sha256=anchor)
        packet_path = bundle / "G1/packet.json"
        packet = json.loads(packet_path.read_bytes())
        packet["previous_packet_sha256"] = "0" * 64
        packet_path.write_bytes(stages.canonical(packet) + b"\n")
        index_path = bundle / "index.json"
        index = json.loads(index_path.read_bytes())
        index["stages"][1]["sha256"] = stages.digest(packet_path.read_bytes())
        index_path.write_bytes(stages.canonical(index) + b"\n")
        refused(lambda: stages.verify(bundle, expected_index_sha256=anchor))
        rewritten_anchor = stages.digest(index_path.read_bytes())
        refused(lambda: stages.verify(bundle, expected_index_sha256=rewritten_anchor))
        plan["stages"]["G0"]["evidence"]["preflight_evidence"] = ["../outside.json"]
        refused(lambda: stages.capture(plan, root, root / "escaped"))
        assert not (root / "escaped").exists()
    print(json.dumps({"status": "PASS", "scope": "immutable snapshots, source drift, overwrite refusal, tamper, dependency chain and traversal; not semantic stage approval"}))
if __name__ == "__main__":
    main()
