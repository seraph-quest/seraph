"""Machine checked inventory of direct inference and provider transports.

The model fabric is the runtime choke point for canonical inference, but a few
provider transports remain in the repository for the legacy model wrapper and
for bounded health/capability probes.  This test keeps those call sites
explicit and reviewable.  It is a source guardrail, not proof that a live
runtime request selected the broker.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

import pytest


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"

_HTTP_METHODS = frozenset(
    {
        "delete",
        "get",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "stream",
    }
)
_LITELLM_DISPATCH_METHODS = frozenset(
    {
        "acompletion",
        "completion",
        "embedding",
        "image_generation",
        "text_completion",
        "transcription",
    }
)


@dataclass(frozen=True, slots=True)
class DispatchSite:
    """One source-level direct provider or HTTP transport call."""

    path: str
    line: int
    symbol: str
    operation: str


@dataclass(frozen=True, slots=True)
class DispatchReview:
    """The review record required for every known direct dispatch site."""

    path: str
    line: int
    symbol: str
    operation: str
    classification: str
    reason: str
    expected_future_migration: str


def _attribute_chain(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _target_names(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in node.elts:
            names.update(_target_names(item))
        return names
    return set()


def _function_symbol(scope: tuple[str, ...]) -> str:
    return ".".join(scope) if scope else "<module>"


class _DispatchVisitor(ast.NodeVisitor):
    """Find provider calls and methods on clients constructed by httpx.

    This is deliberately a bounded syntactic check.  It follows direct
    imports, context-manager bindings, and simple assignments.  It does not
    claim to infer arbitrary dynamic aliases or calls hidden behind ``eval``.
    A new direct call in one of the recognized forms must be registered before
    this test can pass.
    """

    def __init__(self, *, path: str, tree: ast.AST) -> None:
        self.path = path
        self.scope: list[str] = []
        self.sites: list[DispatchSite] = []
        self.litellm_modules: set[str] = {"litellm"}
        self.litellm_functions: dict[str, str] = {}
        self.httpx_modules: set[str] = {"httpx"}
        self.httpx_client_types: set[str] = {"Client", "AsyncClient"}
        self.httpx_client_bindings: set[str] = set()
        self._collect_bindings(tree)

    def _collect_bindings(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "litellm":
                        self.litellm_modules.add(alias.asname or "litellm")
                    if alias.name == "httpx":
                        self.httpx_modules.add(alias.asname or "httpx")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "litellm":
                    for alias in node.names:
                        if alias.name in _LITELLM_DISPATCH_METHODS:
                            self.litellm_functions[alias.asname or alias.name] = alias.name
                elif module == "httpx":
                    for alias in node.names:
                        if alias.name in self.httpx_client_types:
                            self.httpx_client_types.add(alias.asname or alias.name)

            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if self._is_httpx_client_constructor(item.context_expr):
                        self.httpx_client_bindings.update(_target_names(item.optional_vars))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if self._is_httpx_client_constructor(value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        self.httpx_client_bindings.update(_target_names(target))

    def _is_httpx_client_constructor(self, node: ast.AST | None) -> bool:
        if not isinstance(node, ast.Call):
            return False
        chain = _attribute_chain(node.func)
        if not chain:
            return False
        if len(chain) == 2 and chain[0] in self.httpx_modules:
            return chain[1] in {"Client", "AsyncClient"}
        return len(chain) == 1 and chain[0] in self.httpx_client_types

    def _is_httpx_client_receiver(self, node: ast.AST) -> bool:
        if self._is_httpx_client_constructor(node):
            return True
        return isinstance(node, ast.Name) and node.id in self.httpx_client_bindings

    def _add(self, node: ast.Call, *, operation: str) -> None:
        self.sites.append(
            DispatchSite(
                path=self.path,
                line=node.lineno,
                symbol=_function_symbol(tuple(self.scope)),
                operation=operation,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        chain = _attribute_chain(node.func)
        if chain and len(chain) == 2 and chain[0] in self.litellm_modules:
            if chain[1] in _LITELLM_DISPATCH_METHODS:
                self._add(node, operation=".".join(chain))
        elif isinstance(node.func, ast.Name) and node.func.id in self.litellm_functions:
            self._add(node, operation=f"litellm.{self.litellm_functions[node.func.id]}")

        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method == "generate" and self._looks_like_model_receiver(node.func.value):
                self._add(node, operation=f"{ast.unparse(node.func.value)}.generate")
            elif method in _HTTP_METHODS and self._is_httpx_client_receiver(node.func.value):
                self._add(node, operation=f"{ast.unparse(node.func.value)}.{method}")
        self.generic_visit(node)

    @staticmethod
    def _looks_like_model_receiver(node: ast.AST) -> bool:
        chain = _attribute_chain(node)
        if chain and chain[-1].endswith("LiteLLMModel"):
            return True
        if isinstance(node, ast.Name):
            return "model" in node.id.lower()
        return False


def scan_source(path: str, source: str) -> tuple[DispatchSite, ...]:
    """Scan one source string so tests can prove unauthorized synthetic calls fail."""
    tree = ast.parse(source, filename=path)
    visitor = _DispatchVisitor(path=path, tree=tree)
    visitor.visit(tree)
    return tuple(sorted(visitor.sites, key=lambda item: (item.path, item.line, item.symbol, item.operation)))


def scan_backend_source(root: Path = BACKEND_SRC) -> tuple[DispatchSite, ...]:
    sites: list[DispatchSite] = []
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root).as_posix()
        sites.extend(scan_source(relative_path, path.read_text(encoding="utf-8")))
    return tuple(sorted(sites, key=lambda item: (item.path, item.line, item.symbol, item.operation)))


def _review(
    path: str,
    line: int,
    symbol: str,
    operation: str,
    classification: str,
    reason: str,
    expected_future_migration: str,
) -> DispatchReview:
    return DispatchReview(
        path=path,
        line=line,
        symbol=symbol,
        operation=operation,
        classification=classification,
        reason=reason,
        expected_future_migration=expected_future_migration,
    )


_CANONICAL_ADAPTERS = (
    _review(
        "llm_runtime.py",
        1532,
        "_governed_openai_chat_completion",
        "client.post",
        "canonical_adapter",
        "Leaf OpenAI-compatible transport invoked only after model-fabric preflight and bounded remote-inference admission.",
        "Retain as a leaf adapter; new inference adapters must be called through model_fabric.execution or the shared remote-inference broker.",
    ),
    _review(
        "llm_runtime.py",
        4406,
        "stream_completion_with_fallback.default_transport",
        "client.stream",
        "canonical_adapter",
        "Leaf streaming transport owned by the governed model-fabric execution path; redirects remain disabled and OpenRouter-only mode blocks unregistered callers.",
        "Retain as a leaf adapter; any replacement must preserve admission, deadline, and route-receipt ownership.",
    ),
    _review(
        "memory/embedder.py",
        467,
        "_request_embeddings",
        "client.post",
        "canonical_adapter",
        "Leaf OpenRouter embeddings transport called only after explicit memory cloud-egress policy validation and bounded remote-inference admission; response vectors are schema-checked before use.",
        "Retain as a leaf adapter; add canonical model-fabric capability proofs and durable cost/route receipts through the shared memory-embedding contract before broadening workloads.",
    ),
    _review(
        "observer/screenshot_semantic_analysis.py",
        483,
        "_analyze_with_openrouter._transport",
        "client.post",
        "canonical_adapter",
        "Leaf OpenRouter vision transport called only after explicit screenshot cloud-egress policy, provider controls, bounded image size, and governed route checks.",
        "Retain as a leaf adapter; add durable usage/cost receipts and capability-proof readback before treating a live vision route as generally available.",
    ),
)


_REVIEWED_EXCEPTIONS = (
    _review(
        "llm_runtime.py",
        3528,
        "FallbackLiteLLMModel.generate.invoke_primary_transport",
        "BaseLiteLLMModel.generate",
        "transitional_provider_call",
        "Legacy no-context agent wrapper fallback retained for compatibility; canonical contexts use the governed HTTP adapter instead.",
        "Keep the unreachable compatibility leaf only as a source guard; all active callers must supply an authenticated OpenRouter context and use the governed adapter.",
    ),
    _review(
        "llm_runtime.py",
        3607,
        "FallbackLiteLLMModel.generate.invoke_fallback_transport",
        "fallback_model.generate",
        "transitional_provider_call",
        "Legacy fallback model invocation retained only when the caller has not supplied a canonical request context.",
        "Delete after all legacy no-context callers are removed; active OpenRouter routing never invokes this leaf.",
    ),
    _review(
        "llm_runtime.py",
        4027,
        "completion_with_fallback_sync.invoke_primary_transport",
        "litellm.completion",
        "transitional_provider_call",
        "Legacy completion transport is still reachable when request_context is absent; governed callers use the exact preflighted adapter.",
        "Delete after every completion caller supplies an authenticated canonical OpenRouter context; the active guard rejects this branch.",
    ),
    _review(
        "llm_runtime.py",
        4105,
        "completion_with_fallback_sync.invoke_fallback_transport",
        "litellm.completion",
        "transitional_provider_call",
        "Delete after legacy fallback callers are removed; canonical OpenRouter execution never enters this branch.",
        "Migrate fallback completion attempts to governed model-fabric execution and delete the direct LiteLLM branch.",
    ),
    _review(
        "evals/harness.py",
        5390,
        "_eval_provider_routing_decision_audit",
        "first_model.generate",
        "transitional_provider_call",
        "Deterministic routing evaluation exercises the retained legacy wrapper path with a mocked provider.",
        "Update the evaluation fixture to construct an authenticated canonical context once the legacy wrapper path is removed.",
    ),
    _review(
        "evals/harness.py",
        5399,
        "_eval_provider_routing_decision_audit",
        "rerouted_model.generate",
        "transitional_provider_call",
        "Deterministic routing evaluation exercises legacy reroute behavior with a mocked provider.",
        "Update the evaluation fixture to assert governed reroute receipts after canonical model-fabric migration.",
    ),
    _review(
        "api/model_fabric_settings.py",
        504,
        "_execute_canary_transport",
        "client.post",
        "capability_probe",
        "Manual capability canary sends a bounded fixed fixture after authenticated profile and policy checks.",
        "Move probe transport into a capability-probe adapter that explicitly acquires the bounded remote-inference lease; preserve no-fallback and probe receipts.",
    ),
    _review(
        "api/model_fabric_settings.py",
        516,
        "_execute_canary_transport",
        "client.stream",
        "capability_probe",
        "Manual streaming capability canary observes one bounded fixed response to produce a capability proof.",
        "Move streaming probe execution behind an admitted capability-probe adapter with the same bounded proof contract.",
    ),
    _review(
        "api/model_fabric_settings.py",
        526,
        "_execute_canary_transport",
        "client.post",
        "capability_probe",
        "Manual non-streaming chat capability canary sends a bounded fixed fixture to validate response shape.",
        "Move probe transport behind the remote-inference capability-probe admission path and retain exact proof persistence.",
    ),
    _review(
        "local_runtime_profile_verifier.py",
        221,
        "_verify_profile",
        "client.post",
        "capability_probe",
        "Operator profile verifier performs bounded empirical chat checks and writes a redacted proof receipt.",
        "Route empirical verification through an admitted capability-probe adapter before treating it as shared routing evidence.",
    ),
    _review(
        "local_runtime_profile_verifier.py",
        269,
        "_probe_backend",
        "client.get",
        "health_or_metadata_probe",
        "Read-only health and model metadata probe; it does not request generation.",
        "Keep outside inference admission while read-only; if the probe gains generation, use capability-probe admission.",
    ),
    _review(
        "vlm_runtime.py",
        221,
        "_probe_json_endpoint",
        "client.get",
        "health_or_metadata_probe",
        "Read-only VLM wrapper health, backend-health, or queue-status probe.",
        "Keep outside inference admission while read-only and retain redacted operator status; generation must use the governed adapter.",
    ),
    _review(
        "vlm_runtime.py",
        269,
        "_probe_chat_health",
        "client.get",
        "health_or_metadata_probe",
        "Read-only VLM chat-health probe that reports enabled/auth/model state without generating output.",
        "Keep outside inference admission while read-only; any active canary belongs to capability-probe admission.",
    ),
    _review(
        "scheduler/jobs/end_of_day_goal_report.py",
        723,
        "_deliver_report_resend_template",
        "client.post",
        "non_model_transport",
        "Outbound email delivery to the configured Resend integration; this is not model inference.",
        "No model-fabric migration; keep separately governed as an external delivery capability and reclassify if its endpoint changes.",
    ),
    _review(
        "tools/shell_tool.py",
        39,
        "run_sandboxed_code",
        "client.post",
        "non_model_transport",
        "Outbound request to the sandbox evaluator; this is code execution transport, not model inference.",
        "No model-fabric migration; keep behind the sandbox capability policy and reclassify if the endpoint becomes an inference route.",
    ),
)

_ALL_REVIEWS = tuple(sorted((*_CANONICAL_ADAPTERS, *_REVIEWED_EXCEPTIONS), key=lambda item: (item.path, item.line, item.symbol, item.operation)))


def _site_key(site: DispatchSite | DispatchReview) -> tuple[str, int, str, str]:
    return site.path, site.line, site.symbol, site.operation


def _unreviewed_sites(sites: Iterable[DispatchSite], reviews: Iterable[DispatchReview]) -> tuple[DispatchSite, ...]:
    reviewed = {_site_key(item) for item in reviews}
    return tuple(item for item in sites if _site_key(item) not in reviewed)


def render_inventory(sites: Iterable[DispatchSite], reviews: Iterable[DispatchReview]) -> str:
    """Render a stable, redacted inventory suitable for CI receipts."""
    review_by_key = {_site_key(item): item for item in reviews}
    rows = []
    for site in sorted(sites, key=lambda item: _site_key(item)):
        review = review_by_key.get(_site_key(site))
        rows.append(
            {
                "classification": review.classification if review else "UNREVIEWED",
                "expected_future_migration": review.expected_future_migration if review else "REGISTER_REQUIRED",
                "line": site.line,
                "operation": site.operation,
                "path": site.path,
                "reason": review.reason if review else "Direct dispatch has no review record.",
                "symbol": site.symbol,
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True) + "\n"


def test_direct_dispatch_inventory_is_exact_and_reviewable():
    sites = scan_backend_source()
    assert _unreviewed_sites(sites, _ALL_REVIEWS) == (), render_inventory(sites, _ALL_REVIEWS)
    assert {_site_key(item) for item in sites} == {_site_key(item) for item in _ALL_REVIEWS}
    assert all(item.reason and item.expected_future_migration for item in _ALL_REVIEWS)
    assert render_inventory(sites, _ALL_REVIEWS) == render_inventory(tuple(reversed(sites)), tuple(reversed(_ALL_REVIEWS)))


@pytest.mark.parametrize(
    "source",
    [
        "import litellm\n\ndef new_route(messages):\n    return litellm.completion(messages=messages)\n",
        "from litellm import completion as ask\n\ndef new_route(messages):\n    return ask(messages=messages)\n",
        "import httpx\n\ndef new_route(candidate):\n    with httpx.Client() as client:\n        return client.post(candidate.endpoint)\n",
        "def new_route(model):\n    return model.generate([])\n",
    ],
)
def test_synthetic_unregistered_direct_dispatch_is_rejected(source: str):
    sites = scan_source("synthetic/new_route.py", source)
    assert sites
    assert _unreviewed_sites(sites, _ALL_REVIEWS) == sites


def test_inventory_is_redacted_and_contains_no_source_payloads():
    rendered = render_inventory(scan_backend_source(), _ALL_REVIEWS)
    assert "api_key" not in rendered
    assert "Authorization" not in rendered
    assert "messages=" not in rendered
    assert "candidate.endpoint" not in rendered
