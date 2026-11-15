"""Source inventory of every turn constructor and model caller (#1020).

A migration-completeness check, not evidence of model quality: each site is
keyed by (file, enclosing function, callee), never by line number, and must
map to exactly one category in ``runtime_inference_inventory.json``.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
# Callees whose call constructs an agent or a model, or builds a governed inference request.
CALL_TARGETS = frozenset({"ToolCallingAgent", "CodeAgent", "MultiStepAgent", "FallbackLiteLLMModel",
                          "build_model_kwargs", "build_canonical_inference_context", "GpuAdmissionRequest",
                          "run_capability_probe", "invoke_near_text"})
# The model owner's entry points: any reference counts, including passing one as a value.
REFERENCE_TARGETS = frozenset({"completion_with_fallback", "completion_with_fallback_sync",
                               "stream_completion_with_fallback", "_governed_research_chat_completion",
                               "_governed_openai_chat_completion"})


class _Scanner(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative, self.scope, self.aliases, self.sites = relative, [], {}, []

    def visit_ImportFrom(self, node):
        for name in node.names:
            if name.name in CALL_TARGETS | REFERENCE_TARGETS:
                self.aliases[name.asname or name.name] = name.name

    def _function(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _function

    def _resolve(self, node) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _add(self, callee: str, route: str | None = None):
        self.sites.append((self.relative, ".".join(self.scope) or "<module>", callee if route is None
                           else f"{callee}:{route}"))

    def visit_Call(self, node):
        callee = self._resolve(node.func)
        if callee in CALL_TARGETS:
            route = None
            if callee == "build_canonical_inference_context" and node.args and isinstance(node.args[0], ast.Constant):
                route = node.args[0].value
            if callee == "build_model_kwargs":
                route = next((keyword.value.value for keyword in node.keywords if keyword.arg == "runtime_path"
                              and isinstance(keyword.value, ast.Constant)), None)
            self._add(callee, route)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and self.aliases.get(node.id, node.id) in REFERENCE_TARGETS:
            self._add(self.aliases.get(node.id, node.id))

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load) and node.attr in REFERENCE_TARGETS:
            self._add(node.attr)
        self.generic_visit(node)


def scan() -> dict[str, int]:
    """Every site with its occurrence count, keyed ``file::function::callee``."""
    counts: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        scanner = _Scanner(relative)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        for site in scanner.sites:
            key = "::".join(site)
            counts[key] = counts.get(key, 0) + 1
    return counts

AGENT_CLASSES = frozenset({"ToolCallingAgent", "CodeAgent", "MultiStepAgent"})
# #1115: the one sanctioned agent class, which re-raises control-flow interrupts.
SANCTIONED_AGENT_MODULE = "src.agent.tool_calling_agent"


def agent_constructions() -> list[tuple[str, str, str, str]]:
    """Every agent construction as (file, function, class, module it was imported from)."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        origins, modules = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for name in node.names:
                    if name.name in AGENT_CLASSES:
                        origins[name.asname or name.name] = (node.module, name.name)
            elif isinstance(node, ast.Import):
                for name in node.names:
                    modules[name.asname or name.name] = name.name

        def visit(node, scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = scope + [node.name]
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in origins:
                    module, cls = origins[func.id]
                    found.append((relative, ".".join(scope) or "<module>", cls, module))
                elif (isinstance(func, ast.Attribute) and func.attr in AGENT_CLASSES
                      and isinstance(func.value, ast.Name)):
                    found.append((relative, ".".join(scope) or "<module>", func.attr,
                                  modules.get(func.value.id, func.value.id)))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        visit(tree, [])
    return found
