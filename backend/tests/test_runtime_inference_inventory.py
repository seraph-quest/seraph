"""#1020: every turn constructor and model caller maps to one composed owner.

A source inventory is a migration-completeness check, not evidence of model
quality. Sites are keyed by (file, enclosing function, callee) with import
aliases resolved; a new unmapped site, a stale entry or a changed count fails.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from tests.runtime_inference_inventory import SRC, scan

INVENTORY = json.loads((Path(__file__).with_name("runtime_inference_inventory.json")).read_text())


def test_every_site_is_mapped_and_no_entry_is_stale():
    found = scan()
    mapped = INVENTORY["sites"]
    assert sorted(set(found) - set(mapped)) == [], "unmapped model or turn sites"
    assert sorted(set(mapped) - set(found)) == [], "stale inventory entries"
    assert {key: value["count"] for key, value in mapped.items()} == found
    assert {value["category"] for value in mapped.values()} <= set(INVENTORY["categories"])


def test_every_registered_route_has_one_owner_category():
    from src.agent.presets import PRESETS
    from src.model_fabric.caller_context import CANONICAL_ROUTE_SPECS
    assert set(INVENTORY["routes"]) == set(CANONICAL_ROUTE_SPECS)
    assert set(INVENTORY["routes"].values()) <= set(INVENTORY["categories"])
    # Presets never invent a route: each names a registered one. The MCP rule preset names the
    # per-server pattern ``mcp_<server>``, whose concrete routes are the specialist route family.
    fixed = {value["runtime_path"] for value in PRESETS.values() if "<" not in value["runtime_path"]}
    assert fixed <= set(CANONICAL_ROUTE_SPECS)
    assert [value["runtime_path"] for value in PRESETS.values() if "<" in value["runtime_path"]] == ["mcp_<server>"]


def _functions_using(names: set[str]) -> set[tuple[str, str]]:
    """(file, top-level function) pairs whose body references one of ``names``."""
    result = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node)):
                    result.add((path.relative_to(SRC).as_posix(), node.name))
    return result


def test_guardian_sites_run_inside_a_guardian_turn():
    """Each unattended call site is wrapped by the guardian loop, never called bare."""
    wrapped = _functions_using({"guardian_completion", "guardian_completion_fn", "_report_completion"})
    for key, value in INVENTORY["sites"].items():
        if value["category"] != "guardian_turn":
            continue
        relative, function, _ = key.split("::")
        top = function.split(".")[0]
        if relative == "memory/pipeline/extract.py":
            # Called only with the guardian-wrapped completion_fn from the consolidator.
            continue
        assert (relative, top) in wrapped, key


def test_agent_constructors_are_only_the_reviewed_facades():
    """smolagents constructions stay in the agent factories (turns attach to them) and eval facades."""
    constructors = {key for key in INVENTORY["sites"] if key.endswith("::ToolCallingAgent")
                    or key.endswith("::CodeAgent") or key.endswith("::MultiStepAgent")}
    assert {key.split("::")[0] for key in constructors} <= {"agent/factory.py", "agent/onboarding.py",
                                                             "agent/specialists.py", "agent/strategist.py"}
    assert INVENTORY["sites"]["agent/strategist.py::create_strategist_agent::ToolCallingAgent"]["category"] == "eval_facade"


def test_the_strategist_facade_is_referenced_only_by_evals():
    """create_strategist_agent keeps its three read tools but is never a production turn."""
    users = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative in {"agent/strategist.py"} or relative.startswith("evals/"):
            continue
        if "create_strategist_agent" in path.read_text(encoding="utf-8"):
            users.append(relative)
    assert users == []


def test_every_agent_construction_is_the_sanctioned_subclass():
    """#1115: only src.agent.tool_calling_agent.ToolCallingAgent re-raises approval and clarification
    interrupts; constructing smolagents' class (or another agent class) directly would swallow them."""
    from tests.runtime_inference_inventory import SANCTIONED_AGENT_MODULE, agent_constructions
    constructions = agent_constructions()
    assert constructions, "the scan found no agent construction"
    assert [item for item in constructions if (item[2], item[3]) != ("ToolCallingAgent", SANCTIONED_AGENT_MODULE)] == []
    # smolagents' class appears only as the sanctioned subclass's base.
    source = (SRC / "agent/tool_calling_agent.py").read_text(encoding="utf-8")
    assert "class ToolCallingAgent(_SmolagentsToolCallingAgent):" in source
