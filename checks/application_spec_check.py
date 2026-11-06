#!/usr/bin/env python3
"""Behavioral checks for governed specification validation and phase contracts."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import copy
import json
import sys
from pathlib import Path
import application_spec as spec


def fixture():
    return {
        "schema": 1, "spec_id": "inventory", "version": 1, "goal": "Manage inventory items",
        "requirements": [{"id": "R-ADD", "statement": "Add an item", "provenance": "user",
            "source": "authoritative goal", "inputs": ["sku", "quantity"], "outputs": ["item"],
            "failures": ["invalid item rejected"], "invariants": ["quantity is nonnegative"],
            "state_transitions": ["absent to present on valid add"], "non_goals": ["multi-warehouse"],
            "acceptance_ids": ["AC-ADD"], "ambiguity_ids": [], "conflict_ids": []}],
        "entities": [{"id": "Item", "fields": [{"name": "sku", "type": "string", "required": True,
            "constraints": ["nonempty"], "provenance": "user", "source": "authoritative goal"}],
            "constraints": ["sku unique"], "roles": ["operator"]}],
        "tasks": [{"id": "T-DESIGN", "title": "Define item contract", "depends_on": [],
            "requirement_ids": ["R-ADD"], "acceptance_ids": ["AC-ADD"], "kind": "design"},
            {"id": "T-VERIFY", "title": "Verify add behavior", "depends_on": ["T-DESIGN"],
            "requirement_ids": ["R-ADD"], "acceptance_ids": ["AC-ADD"], "kind": "verification"}],
        "oracle": {"public_acceptance_ids": ["AC-ADD"], "hidden_oracle_ids": ["H-1"],
            "separation": "Hidden evaluator answers are unavailable to proposal and task planning."},
        "ambiguities": [], "conflicts": [], "non_goals": ["No authentication"],
        "approval": {"status": "draft", "operator": "", "breaking_change_authorized": False,
                     "authorization_source": ""}}


def rejects(fn, label):
    try:
        fn()
    except spec.SpecRefused:
        return
    raise AssertionError(f"{label} was not rejected")


def _tool_result(name, result, call_id="spec-call"):
    return {"tool_calls": [{"id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(result)}}]}


def check_controller_bound_goal():
    context = "Use an operator-selected local SQLite file and explicit JSON envelope."
    goal = "\n  Build something.  "
    captured = {"calls": []}
    def fake(messages, tools, on_event=None):
        captured["calls"].append((messages, tools))
        name = tools[0]["function"]["name"]
        if name == "submit_requirements":
            result = {"requirements": fixture()["requirements"], "public_acceptance_ids": ["AC-ADD"],
                "ambiguities": [], "conflicts": [], "non_goals": ["No auth"]}
        elif name == "submit_entities":
            result = {"entities": fixture()["entities"]}
        elif name == "submit_tasks":
            result = {"tasks": fixture()["tasks"]}
        else:
            raise AssertionError(name)
        return _tool_result(name, result)
    original = spec.agent_transport.complete
    spec.agent_transport.complete = fake
    try:
        feedback = "Public review: split source provenance, model domain Item entity, and add independent verification."
        result = spec.propose_from_goal(goal, design_context=context,
            hidden_oracle_ids=["hidden-private"], public_acceptance_ids=["AC-ADD"], review_feedback=feedback)
        assert [x[1][0]["function"]["name"] for x in captured["calls"]] == [
            "submit_requirements", "submit_entities", "submit_tasks"]
        assert result["goal"] == goal and result["oracle"]["hidden_oracle_ids"] == ["hidden-private"]
        assert all(feedback in messages[-1]["content"] for messages, _tools in captured["calls"])
        assert all("hidden-private" not in json.dumps(messages) for messages, _tools in captured["calls"])
        assert result["design_context"] == context
        assert all("hidden-private" not in json.dumps(messages) and
                   "hidden-private" not in json.dumps(tools) for messages, tools in captured["calls"])
        first_messages, first_tools = captured["calls"][0]
        assert any(goal in message.get("content", "") for message in first_messages)
        assert first_tools[0]["function"]["parameters"]["properties"]["public_acceptance_ids"]["items"]["enum"] == ["AC-ADD"]
        assert "approval" not in first_tools[0]["function"]["parameters"]["properties"]
        assert "hidden_oracle_ids" not in first_tools[0]["function"]["parameters"]["properties"]
        changed = copy.deepcopy(result); changed["design_context"] = "tampered"
        rejects(lambda: spec.validate_spec(changed), "design-context digest tampering")
        rejects(lambda: spec.propose_from_goal(goal, design_context=123), "nontext design context")
        rejects(lambda: spec.propose_from_goal(goal, design_context="中" * 10923), "oversized UTF-8 design context")
        for altered_ids in (["AC-OTHER"], ["AC-ADD", "AC-OTHER"], [], ["AC-ADD", "AC-ADD"], ["AC-ADD", None]):
            def altered(messages, tools, on_event=None, ids=altered_ids):
                name = tools[0]["function"]["name"]
                if name != "submit_requirements": raise AssertionError("later stage should not run")
                proposal = {"requirements": fixture()["requirements"], "public_acceptance_ids": ids,
                    "ambiguities": [], "conflicts": [], "non_goals": []}
                return _tool_result(name, proposal, "bad-spec")
            spec.agent_transport.complete = altered
            rejects(lambda: spec.propose_from_goal(goal, public_acceptance_ids=["AC-ADD"]),
                    "provider-altered public acceptance IDs")
    finally:
        spec.agent_transport.complete = original


def check_issue_early_stop():
    goal = "Exact original goal"
    context = "Controller design specifies the compatible implementation choices."
    original = spec.agent_transport.complete
    def blocked(messages, tools, on_event=None):
        if tools[0]["function"]["name"] != "submit_requirements":
            raise AssertionError("blocking issue reached a later stage")
        reqs = copy.deepcopy(fixture()["requirements"])
        reqs[0]["ambiguity_ids"] = ["A1"]
        result = {"requirements": reqs, "public_acceptance_ids": ["AC-ADD"],
            "ambiguities": [{"id": "A1", "description": "Need clarification", "blocking": True}],
            "conflicts": [], "non_goals": []}
        return _tool_result("submit_requirements", result, "blocked")
    spec.agent_transport.complete = blocked
    try:
        try:
            spec.propose_from_goal(goal, design_context=context, hidden_oracle_ids=["hidden-private"],
                                   public_acceptance_ids=["AC-ADD"])
        except spec.SpecRefused as exc:
            evidence = exc.proposal_stage
            assert evidence["goal"] == goal and evidence["design_context"] == context
            assert evidence["approved"] is False and evidence["semantically_validated"] is False
            assert evidence["result"]["ambiguities"][0]["id"] == "A1"
            assert "hidden-private" not in json.dumps(evidence)
        else:
            raise AssertionError("blocking ambiguity reached planning")
    finally:
        spec.agent_transport.complete = original


def check_task_stage_repair():
    original=spec.agent_transport.complete
    calls=[]
    def model(messages,tools,on_event=None):
        name=tools[0]["function"]["name"]; calls.append(name)
        base=fixture()
        if name=="submit_requirements": result={"requirements":base["requirements"],"public_acceptance_ids":["AC-ADD"],"ambiguities":[],"conflicts":[],"non_goals":[]}
        elif name=="submit_entities": result={"entities":base["entities"]}
        else:
            tasks=copy.deepcopy(base["tasks"])
            if calls.count("submit_tasks")==1:
                tasks[0]["requirement_ids"]=["UNKNOWN"]
            result={"tasks":tasks}
        return _tool_result(name,result,f"stage-{len(calls)}")
    observed=[]
    spec.agent_transport.complete=model
    try:
        draft=spec.propose_from_goal("Exact goal",public_acceptance_ids=["AC-ADD"],on_event=observed.append)
        assert calls==["submit_requirements","submit_entities","submit_tasks","submit_tasks"]
        assert draft["tasks"][0]["requirement_ids"]==["R-ADD"]
        assert any(e.get("type")=="proposal_correction" and e.get("stage")=="submit_tasks" for e in observed)
    finally:
        spec.agent_transport.complete=original


def check_bounded_correction():
    original = spec.agent_transport.complete
    attempts = []
    def model(messages, tools, on_event=None):
        name = tools[0]["function"]["name"]; attempts.append(name)
        if name == "submit_requirements":
            result = {"requirements": fixture()["requirements"], "public_acceptance_ids": ["AC-ADD"],
                "ambiguities": [], "conflicts": [], "non_goals": ["No auth"]}
            if attempts.count(name) == 1:
                result["non_goals"] = ["No auth", "No auth"]
        elif name == "submit_entities":
            result = {"entities": fixture()["entities"]}
        else:
            result = {"tasks": fixture()["tasks"]}
        return _tool_result(name, result, f"stage-{len(attempts)}")
    spec.agent_transport.complete = model
    try:
        draft = spec.propose_from_goal("Exact original goal", design_context="Exact design",
                                       hidden_oracle_ids=["private-id"])
        assert attempts == ["submit_requirements", "submit_requirements", "submit_entities", "submit_tasks"]
        assert draft["goal"] == "Exact original goal" and draft["design_context"] == "Exact design"
        assert draft["approval"]["status"] == "draft"
        attempts.clear()
        def invalid(messages, tools, on_event=None):
            attempts.append(tools[0]["function"]["name"])
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"bad-{len(attempts)}",
                "type": "function", "function": {"name": "submit_requirements", "arguments": "{}"}}]}
        spec.agent_transport.complete = invalid
        rejects(lambda: spec.propose_from_goal("Exact original goal"), "correction exhaustion")
        assert attempts == ["submit_requirements"] * 3
        attempts.clear()
        def issue(messages, tools, on_event=None):
            attempts.append("requirements")
            result = {"requirements": fixture()["requirements"], "public_acceptance_ids": ["AC-ADD"],
                "ambiguities": [{"id": "A1", "description": "Unresolved business choice", "blocking": True}],
                "conflicts": [], "non_goals": ["duplicate", "duplicate"]}
            return _tool_result("submit_requirements", result, "issue")
        spec.agent_transport.complete = issue
        rejects(lambda: spec.propose_from_goal("Exact original goal"), "issues must bypass correction")
        assert attempts == ["requirements"]
    finally:
        spec.agent_transport.complete = original


def check_bindings():
    bad = fixture(); bad["tasks"][0].pop("requirement_ids")
    rejects(lambda: spec.validate_spec(bad), "missing task requirement ID")
    bad = fixture(); bad["tasks"][0]["requirement_ids"] = ["UNKNOWN"]
    rejects(lambda: spec.validate_spec(bad), "unknown task requirement ID")
    bad = fixture(); bad["requirements"].append({**copy.deepcopy(bad["requirements"][0]),
        "id": "R-OTHER", "acceptance_ids": ["AC-OTHER"]})
    bad["oracle"]["public_acceptance_ids"].append("AC-OTHER")
    bad["tasks"][0].update(requirement_ids=["R-ADD"], acceptance_ids=["AC-OTHER"])
    rejects(lambda: spec.validate_spec(bad), "acceptance not bound to named requirement")
    bad = fixture(); bad["ambiguities"] = [{"id": "A1", "description": "Unspecified behavior", "blocking": True}]
    rejects(lambda: spec.validate_spec(bad), "unlinked ambiguity")
    bad = fixture(); bad["requirements"][0]["ambiguity_ids"] = ["A1"]
    bad["ambiguities"] = [{"id": "A1", "description": "Unspecified behavior", "blocking": True}]
    rejects(lambda: spec.validate_spec(bad), "linked unresolved ambiguity")
    bad = fixture(); bad["requirements"][0]["provenance"] = "model"
    rejects(lambda: spec.validate_spec(bad), "invalid provenance category")
    bad = fixture(); bad["tasks"][0]["requirement_ids"] = ["R-ADD", "R-ADD"]
    rejects(lambda: spec.validate_spec(bad), "duplicate task requirement ID")


def main():
    check_controller_bound_goal()
    check_issue_early_stop()
    check_bounded_correction()
    check_task_stage_repair()
    check_bindings()
    draft = spec.propose_spec(fixture())
    assert draft["approval"]["status"] == "draft" and len(draft["sha256"]) == 64
    assert spec.validate_spec(draft)["sha256"] == draft["sha256"]
    assert draft["requirements"][0]["provenance"] == "user"
    assert draft["requirements"][0]["source"] == "authoritative goal"
    approved = spec.approve_spec(draft, operator="operator-7", authorization_source="approval ticket 123")
    rejects(lambda: spec.validate_spec(draft, require_approved=True), "unapproved spec")
    revised = copy.deepcopy(fixture()); revised["version"] = 2
    retained = spec.revise_spec(approved, revised, operator="operator-7", authorization_source="ticket")
    assert retained["revision"]["retained_acceptance_ids"] == ["AC-ADD"]
    assert retained["revision"]["previous_sha256"] == approved["sha256"]
    rejects(lambda: spec.revise_spec(approved, fixture(), operator="operator-7", authorization_source="ticket"),
            "version must increment")
    revised["requirements"] = [{"id": "R-REPLACE", "statement": "Replace item behavior", "provenance": "user",
        "source": "authoritative goal", "inputs": ["item"], "outputs": ["replacement"],
        "failures": ["invalid replacement rejected"], "invariants": ["replacement is valid"],
        "state_transitions": ["item changes on replacement"], "non_goals": [], "acceptance_ids": ["AC-REPLACE"],
        "ambiguity_ids": [], "conflict_ids": []}]
    revised["oracle"]["public_acceptance_ids"] = ["AC-REPLACE"]
    for task in revised["tasks"]:
        task["requirement_ids"] = ["R-REPLACE"]
        task["acceptance_ids"] = ["AC-REPLACE"]
    rejects(lambda: spec.revise_spec(approved, revised, operator="operator-7", authorization_source="ticket"),
            "regression deletion")
    authorized_draft = spec.revise_spec(approved, revised, operator="operator-7", authorization_source="ticket",
                                        breaking_change_authorized=True)
    authorized = spec.approve_spec(authorized_draft, operator="operator-7", authorization_source="breaking ticket",
                                   breaking_change_authorized=True)
    assert authorized["revision"]["removed_acceptance_ids"] == ["AC-ADD"]
    assert authorized["revision"]["operator"] == "operator-7"
    gates = spec.phase_gates()
    assert [g["id"] for g in gates] == ["G0", "G1", "G2", "G3", "G4"]
    assert gates[4]["depends_on"] == ["G3"]
    assert all(g["blocked_is_reported"] and g["entry_conditions"] and g["outputs"] and g["exit_conditions"] for g in gates)
    assert gates[1]["excluded_requirements"] == ["multiple providers", "GUI", "full context compression", "multi-agent"]
    print("application_spec_check: PASS (staged proposal, hidden-oracle separation, bounded correction, issue early-stop, requirement/task binding, versioning, approval and A13 gates)")


if __name__ == "__main__":
    main()
