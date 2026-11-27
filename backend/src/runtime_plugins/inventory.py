"""The source inventory: every runtime responsibility in ``backend/src`` and its plugin owner (#1205).

Design #1026 §5 and ADR-036 §4/§5. This module is data plus a pure AST scan.
It imports nothing from the runtime, starts nothing and changes no
composition. ``tests/test_source_inventory_1205.py`` scans ``backend/src``
for lifecycle shapes and fails on any finding that is in none of the three
tables below, and on any table row the scan no longer finds.

Each finding is a ``(module:symbol, shape)`` pair. The shapes are:

* ``lifecycle_method``: a ``def start``/``def stop`` (sync or async). The key
  is the owning class, or the module-level function itself.
* ``scheduler_job``: an ``add_job`` call, and each system-job literal (a dict
  with ``func``, ``trigger`` and ``id``) keyed ``symbol[job=<id>]``.
* ``router``: an ``APIRouter`` (keyed by the variable it is bound to) or a
  route declared straight on the FastAPI ``app``.
* ``websocket``: a ``@router.websocket`` endpoint or a websocket route call.
* ``app_factory``: a ``FastAPI(...)`` construction.
* ``background_task``: ``asyncio.create_task``/``ensure_future`` (or the loop
  form) and ``track_task``.
* ``thread``: a ``threading.Thread``.
* ``subprocess``: ``asyncio.create_subprocess_*``, ``subprocess.*``,
  ``os.fork``/``spawn``/``exec``, ``pty.spawn``, ``multiprocessing.Process``.
* ``lifespan_step``: a step call inside the ``src.app`` lifespan functions,
  keyed ``function[step=<callee>]``.

Every finding lands in exactly one table:

* ``OWNERS``: a responsibility whose plugin owner exists today, i.e. a runtime
  domain row in ``ownership.DOMAINS``. Its bundle is the A3.3-1 bundle that
  ``trust.DOMAIN_BUNDLES`` gives that domain.
* ``GAPS``: a responsibility with no plugin owner yet. It names the planned
  seam or domain, its ADR-036 §4 bundle, the tracked work expected to own it
  and why it is a gap. Gaps are listed, never hidden.
* ``EXCLUSIONS``: not a plugin by decision. Composition bootstrap (ADR-036
  §1), pure utilities, types and contracts, and test helpers. Each carries a
  one-line reason.

To add a capability (for example a #1308 child): give its new lifecycle
shapes rows here. A new domain needs its row in ``ownership.DOMAINS`` and
``trust.DOMAIN_BUNDLES`` first; until then it is a gap.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SRC_ROOT = Path(__file__).resolve().parents[1]

SHAPES = frozenset({"lifecycle_method", "scheduler_job", "router", "websocket", "app_factory", "background_task",
                    "thread", "subprocess", "lifespan_step"})
PROVIDERS = frozenset({"python", "node"})
EXCLUSION_KINDS = frozenset({"bootstrap", "utility", "contract", "test_helper"})

# Planned seams and domains with no runtime domain row yet (design #1026 §2,
# ADR-036 §4). ``None`` is the composition bootstrap itself (ADR-036 §1).
PLANNED_SEAMS: Mapping[str | None, str | None] = MappingProxyType({
    "seraph.settings.v1": "seraph-cockpit", "seraph.http.v1": "seraph-cockpit",
    "seraph.websocket.v1": "seraph-cockpit", "seraph.observer.v1": "seraph-edge",
    "seraph.channels.v1": "seraph-channels", "seraph.browser.v1": "seraph-capabilities",
    "seraph.repository.v1": "seraph-capabilities", "seraph.sandbox.v1": "seraph-capabilities",
    "seraph.documents.v1": "seraph-capabilities", "seraph.external-adapters.v1": "seraph-capabilities",
    None: None,
})
# The work expected to own a gap. The A3.3 slices (#1026 slicing plan), or the
# explicit marker for a capability seam no issue tracks yet (ADR-036 §5 says
# each such move is its own slice).
UNTRACKED_SEAM_SLICE = "untracked: ADR-036 §5 capability seam slice"
GAP_OWNERS = MappingProxyType({
    "#1207": "A3.3-4 passive Python worker entry",
    "#1208": "A3.3-5 interface domains and plugins (settings, http, websocket)",
    "#1209": "A3.3-6 observer and channel interface plugins",
    "#1210": "A3.3-7 Node bootstrap managed entry",
    "#1211": "A3.3-8 retire the self-starting legacy composition",
    UNTRACKED_SEAM_SLICE: "capability seams outside A3.3; needs its own tracked slice",
})


@dataclass(frozen=True)
class Owner:
    key: str
    shapes: frozenset[str]
    domain: str
    bundle: str
    provider: str
    required: bool


@dataclass(frozen=True)
class Gap:
    key: str
    shapes: frozenset[str]
    target: str | None
    bundle: str | None
    provider: str
    required: bool
    owner: str
    reason: str


@dataclass(frozen=True)
class Exclusion:
    key: str
    shapes: frozenset[str]
    kind: str
    reason: str


def _shapes(text: str) -> frozenset[str]:
    return frozenset(text.split())


def _bundle_for(domain: str) -> str:
    from .trust import DOMAIN_BUNDLES
    return DOMAIN_BUNDLES[domain]


def _owned(domain: str, rows: dict[str, str], *, provider: str = "python") -> tuple[Owner, ...]:
    from .trust import TRUST_BUNDLE
    bundle = _bundle_for(domain)
    # Required: a pinned seraph-trust service; its loss fences the tree (ADR-036 §3).
    return tuple(Owner(key, _shapes(shapes), domain, bundle, provider, bundle == TRUST_BUNDLE)
                 for key, shapes in rows.items())


def _gap(target: str | None, owner: str, reason: str, rows: dict[str, str], *, required: bool = False,
         provider: str = "python") -> tuple[Gap, ...]:
    return tuple(Gap(key, _shapes(shapes), target, PLANNED_SEAMS[target], provider, required, owner, reason)
                 for key, shapes in rows.items())


def _excluded(kind: str, rows: dict[str, tuple[str, str]]) -> tuple[Exclusion, ...]:
    return tuple(Exclusion(key, _shapes(shapes), kind, reason) for key, (shapes, reason) in rows.items())


_LS = "src.app:_lifespan_services[step="
_PW = "src.app:_provider_worker_lifespan[step="
_JOB = "src.scheduler.engine:init_scheduler[job="

OWNERS: tuple[Owner, ...] = (
    # seraph-trust (pinned, required).
    *_owned("seraph.state.v1", {
        f"{_PW}attach]": "lifespan_step", f"{_PW}worker.acquire_workspace]": "lifespan_step",
        f"{_PW}worker.activate]": "lifespan_step", f"{_PW}worker.close_gate]": "lifespan_step",
        f"{_PW}worker.close_state]": "lifespan_step", f"{_PW}worker.release_workspace]": "lifespan_step",
        f"{_PW}worker.detach]": "lifespan_step",
    }),
    *_owned("seraph.authority.v1", {f"{_LS}_reconcile_operator_auth_mode]": "lifespan_step"}),
    *_owned("seraph.audit.v1", {
        "src.audit.runtime:log_background_task_event_sync": "background_task",
        "src.audit.runtime:log_integration_event": "background_task",
        "src.audit.runtime:log_integration_event_sync": "background_task",
        "src.llm_runtime:_log_llm_runtime_event_sync": "background_task",
    }),
    *_owned("seraph.tasks.v1", {
        f"{_LS}durable_job_repository.recover_stale_jobs]": "lifespan_step",
        f"{_LS}current_task_service]": "lifespan_step",
        "src.work_board.general_task:GeneralTaskService": "lifecycle_method",
        "src.work_board.general_task:GeneralTaskService.retain_native_invocation.closed": "background_task",
    }),
    *_owned("seraph.inference.v1", {
        f"{_LS}hydrate_openrouter_credential]": "lifespan_step",
        f"{_LS}run_openrouter_route_revoke_at_boot]": "lifespan_step",
        f"{_LS}ModelFabricRepository().cleanup_settled_audio_documentation]": "lifespan_step",
        f"{_LS}init_llm_logging]": "lifespan_step",
        f"{_LS}near_tee_catalog_refresher.start]": "lifespan_step",
        f"{_LS}near_tee_catalog_refresher.stop]": "lifespan_step",
        "src.model_fabric.near_tee_catalog:NearTeeCatalogRefresher": "lifecycle_method",
        "src.model_fabric.near_tee_catalog:NearTeeCatalogRefresher.start": "background_task",
        "src.model_fabric.accounting:DurableInferenceBrokerMixin._before_release": "background_task",
    }),
    # seraph-agent.
    *_owned("seraph.agent-loop.v1", {
        "src.api.chat:chat": "background_task",
        "src.api.chat:_begin_rest_revocation_watch": "background_task",
    }),
    *_owned("seraph.guardian.v1", {
        f"{_LS}current_goal_discovery]": "lifespan_step",
        "src.guardian.goal_discovery:GoalDiscoveryService": "lifecycle_method",
        "src.guardian.opportunity_runtime:run_opportunity_tick": "background_task",
    }),
    *_owned("seraph.capabilities.v1", {
        f"{_LS}default_manifest_roots_for_workspace]": "lifespan_step",
        f"{_LS}skill_manager.init]": "lifespan_step", f"{_LS}runbook_manager.init]": "lifespan_step",
        f"{_LS}workflow_manager.init]": "lifespan_step", f"{_LS}starter_pack_manager.init]": "lifespan_step",
        f"{_LS}routine_service.recover_pending_installs]": "lifespan_step",
        "src.native_tools.task_adapters:ToolRegistry": "lifecycle_method",
        "src.native_tools.task_adapters:ToolRegistry.begin_invocation": "background_task",
        "src.api.evolution:_run_evolution_thread_cancel_safe": "background_task",
    }),
    # seraph-capabilities.
    *_owned("seraph.research.v1", {"src.workflows.research_coordinator:_continue_parent": "background_task"}),
    # seraph-domains.
    *_owned("seraph.goals.v1", {
        f"{_LS}goal_programme_service.start]": "lifespan_step",
        f"{_LS}goal_programme_service.stop]": "lifespan_step",
        "src.guardian.goal_programmes:GoalProgrammeService": "lifecycle_method",
    }),
    *_owned("seraph.memory.v1", {
        f"{_LS}ensure_soul_exists]": "lifespan_step",
        f"{_LS}current_method.start]": "lifespan_step", f"{_LS}current_method.stop]": "lifespan_step",
        f"{_LS}historical_method_service.start]": "lifespan_step",
        f"{_LS}historical_method_service.stop]": "lifespan_step",
        "src.memory.task_methods:CurrentMethod": "lifecycle_method",
        "src.work_board.historical_method:HistoricalMethodService": "lifecycle_method",
        "src.memory.near_embedding_reindex:start_reindex": "background_task",
        "src.memory.task_lessons:create_task_lesson": "background_task",
        "src.memory.task_lessons:create_task_lesson.completed": "background_task",
    }),
    *_owned("seraph.artifacts.v1", {f"{_LS}cleanup_retired_audio_quarantine_at_startup]": "lifespan_step"}),
    *_owned("seraph.conversation.v1", {
        f"{_LS}TaskContinuityService]": "lifespan_step",
        f"{_LS}continuity.start]": "lifespan_step", f"{_LS}continuity.stop]": "lifespan_step",
        f"{_LS}session_manager.bind_task_continuity]": "lifespan_step",
        "src.conversation.task_context:TaskContinuityService": "lifecycle_method",
    }),
    *_owned("seraph.connections.v1", {
        f"{_LS}ConnectionSyncService]": "lifespan_step",
        f"{_LS}connection_sync_runtime.start]": "lifespan_step",
        f"{_LS}connection_sync_runtime.stop]": "lifespan_step",
        f"{_LS}purge_cordis_mail_results]": "lifespan_step",
        "src.integrations.connection_sync:ConnectionSyncService": "lifecycle_method",
        "src.integrations.connection_sync:ConnectionSyncService.synchronize": "background_task",
        "src.integrations.connection_sync:ConnectionSyncService._synchronize.fetch": "background_task",
        "src.integrations.calendar_reschedule_runtime:run_owned": "background_task",
        "src.integrations.mail_reply_runtime:run_owned": "background_task",
        "src.integrations.github_cli:GhCliReader._run": "background_task",
        # Retention-exempt timer (lifecycle.RETENTION_JOBS) over Cordis mail results.
        f"{_JOB}cordis_mail_retention]": "scheduler_job",
    }),
    # seraph-lifecycle. Each gated system job maps to the domain lifecycle.job_domain gives it.
    *_owned("seraph.scheduler.v1", {
        f"{_LS}init_scheduler]": "lifespan_step", f"{_LS}sync_scheduled_jobs]": "lifespan_step",
        f"{_LS}shutdown_scheduler]": "lifespan_step",
        f"{_LS}runtime_lifecycle.start]": "lifespan_step", f"{_LS}runtime_lifecycle.stop]": "lifespan_step",
        "src.runtime_plugins.lifecycle:RuntimeLifecycle": "lifecycle_method",
        "src.scheduler.engine:init_scheduler": "scheduler_job",
        "src.scheduler.engine:sync_scheduled_jobs": "scheduler_job",
        **{f"{_JOB}{job}]": "scheduler_job" for job in (
            "programme_daily_digest", "goal_public_discovery", "memory_consolidation", "goal_check",
            "calendar_scan", "strategist_tick", "guardian_opportunity_assessment", "daily_briefing",
            "evening_review", "activity_digest", "end_of_day_goal_report", "weekly_activity_review",
            "screenshot_folder_ingest", "screenshot_folder_analysis", "screenshot_observation_digest",
            "inference_ledger_verification")},
    }),
    *_owned("seraph.dispatch.v1", {
        f"{_JOB}work_board_dispatch]": "scheduler_job",
        "src.work_board.dispatcher:_await_bounded_model_call": "background_task",
        "src.work_board.dispatcher:_await_communication_model_call": "background_task",
        "src.work_board.communication_preparation:_run_source": "background_task",
    }),
)

_HTTP_ROUTERS = (
    "activity", "approvals", "audit", "auth", "automation", "browser", "calendar", "calendar_reschedule", "canvas",
    "capabilities", "capability_packs", "catalog", "chat", "documents", "evolution", "extensions", "forgejo",
    "goals", "guardian_inbox", "mail", "mcp", "memory", "moltbook", "nodes", "operator", "reports", "sessions",
    "skills", "task_evidence", "tools", "work_board", "workflows",
)
_REPORT_REASON = "operator report route spawns the strategy-claim scan; it is served only through the cockpit API"

GAPS: tuple[Gap, ...] = (
    *_gap("seraph.settings.v1", "#1208",
          "settings, status and configuration repair have no interface domain yet; create_app mounts them",
          {"src.api.settings:router": "router", "src.api.model_fabric_settings:router": "router",
           "src.api.profile:router": "router", "src.api.vault:router": "router",
           "src.api.runtime_composition:router": "router", "src.app:create_app.runtime_status": "router",
           "src.api.settings:_choose_screenshot_folder_with_native_dialog": "subprocess",
           f"{_LS}get_or_create_profile]": "lifespan_step"},
          required=True),
    *_gap("seraph.http.v1", "#1208", "HTTP routers are mounted by create_app, not activated by an http-interface plugin",
          {"src.app:create_app": "app_factory", "src.app:create_app.health": "router",
           "src.api.router:api_router": "router",
           **{f"src.api.{name}:router": "router" for name in _HTTP_ROUTERS},
           "src.extensions.github_followthrough:github_followthrough_router": "router",
           "src.guardian.source_watch:source_watch_router": "router",
           "src.integrations.github_activity:github_activity_router": "router",
           "src.integrations.project_source:project_source_router": "router",
           "src.workflows.repo_publication:router": "router",
           "src.workflows.routines:routine_router": "router"}),
    *_gap("seraph.http.v1", "#1208", "the cockpit home projection starts from the lifespan; no interface domain owns it",
          {"src.operator.home_projection:HomeProjection": "lifecycle_method",
           f"{_LS}home_projection.start]": "lifespan_step", f"{_LS}home_projection.stop]": "lifespan_step"}),
    *_gap("seraph.http.v1", "#1208", _REPORT_REASON,
          {"src.cockpit.post_dp_operator_debugging_recovery:_strategy_claim_scan_command_evidence": "subprocess",
           "src.extensions.post_dp_browser_computer_use_reliability:_run_strategy_claim_scan": "subprocess",
           "src.extensions.post_dp_marketplace_lifecycle_gap_closure:_run_strategy_claim_scan": "subprocess",
           "src.guardian.post_dp_guardian_memory_gap_closure:guardian_memory_false_claim_scan_v2_receipts":
               "subprocess"}),
    *_gap("seraph.websocket.v1", "#1208", "WebSocket endpoints are mounted by create_app; no websocket-interface plugin",
          {"src.api.ws:router": "router", "src.api.ws:websocket_chat": "websocket background_task",
           "src.api.ws:websocket_work_board_events": "websocket background_task",
           "src.api.ws:_authorized_with_timeout": "background_task",
           "src.api.ws:_await_authorized": "background_task"}),
    *_gap("seraph.observer.v1", "#1209", "observer and edge-context adapters run without a consent-scoped observer plugin",
          {"src.api.observer:router": "router", "src.api.selected_context:router": "router",
           "src.observer.manager:ContextManager.refresh": "background_task",
           "src.scheduler.jobs.screenshot_folder_analysis:run_screenshot_folder_analysis": "background_task",
           "src.workflows.selected_context_runtime:upload": "background_task",
           f"{_JOB}screen_cleanup]": "scheduler_job",
           f"{_LS}context_manager.refresh]": "lifespan_step",
           **{f"{_LS}context_manager.update_{mode}_mode]": "lifespan_step"
              for mode in ("interruption", "capture", "tool_policy", "mcp_policy", "approval")}}),
    *_gap("seraph.channels.v1", "#1209", "paired channels are plain routes; no channel-interface plugin activates them",
          {"src.api.telegram:router": "router"}),
    *_gap(None, "#1211",
          "Python spawns the Cordis child from the lifespan; #1210 inverts the parent and #1211 removes this host",
          {"src.runtime_plugins.bridge:CordisHost": "lifecycle_method",
           "src.runtime_plugins.bridge:CordisHost.start": "subprocess",
           "src.runtime_plugins.bridge:CordisHost._task": "background_task",
           f"{_LS}cordis_host.start]": "lifespan_step", f"{_LS}cordis_host.stop]": "lifespan_step"}),
    *_gap("seraph.browser.v1", UNTRACKED_SEAM_SLICE, "browser execution has no seam or domain row yet",
          {"src.browser.sessions:ProfiledInteractionSessions": "lifecycle_method",
           "src.browser.sessions:ProfiledInteractionSessions.open": "background_task",
           "src.browser.task_runner:ProfiledInteractionPage": "lifecycle_method",
           "src.browser.task_runner:ProfiledInteractionPage.start": "background_task",
           "src.browser.task_runner:BrowserTaskRunner._execute": "background_task",
           "src.browser.task_runner:BrowserTaskRunner._close_launch_resources_bounded": "background_task",
           "src.browser.task_runner:BrowserTaskRunner._install_popup_and_download_guards.on_download":
               "background_task",
           "src.browser.task_runner:BrowserTaskRunner._install_popup_and_download_guards.on_popup": "background_task",
           "src.browser.downloads:_settle_browser": "background_task",
           "src.browser.forgejo_form_browser:ForgejoFormBrowser.transact": "background_task",
           "src.browser.forgejo_profile:ForgejoTitleBrowser.submit": "background_task",
           "src.browser.moltbook_private_read:MoltbookPrivateBrowserReader.read.deny_page": "background_task",
           f"{_LS}profiled_interaction_sessions.start]": "lifespan_step",
           f"{_LS}profiled_interaction_sessions.stop]": "lifespan_step"}),
    *_gap("seraph.repository.v1", UNTRACKED_SEAM_SLICE, "repository repair executors have no seam or domain row yet",
          {"src.execution.repo_isolation:_disable_userns_supported": "subprocess",
           "src.execution.repo_isolation:probe": "subprocess",
           "src.execution.repo_node:NodeRepoRepairExecutor.execute_job": "subprocess",
           "src.execution.repo_node:runtime_identity": "subprocess",
           "src.execution.repo_original_producer:run_original_producer": "subprocess",
           "src.execution.repo_publication:_bounded_git": "subprocess",
           "src.execution.repo_publication_supervisor:run": "subprocess",
           "src.execution.repo_sandbox:LocalRepoRepairExecutor._run_supervised_python": "subprocess",
           "src.execution.repo_sandbox:LocalRepoRepairExecutor.iterative_preflight": "subprocess",
           "src.execution.repo_sandbox:_git_numstat_paths": "subprocess",
           "src.execution.repo_supervisor:run_command": "subprocess",
           "src.execution.repo_worker:_run_fixed": "subprocess thread",
           "src.api.workflows:_repo_change_track_sandbox_call": "background_task",
           "src.workflows.repo_repair_source:execute_repository_iteration": "background_task",
           "src.workflows.repo_repair_source:grant_repository_iteration_consent": "background_task",
           "src.workflows.repo_repair_source:run_repository_iteration": "background_task"}),
    *_gap("seraph.sandbox.v1", UNTRACKED_SEAM_SLICE, "process and tool-package execution has no seam or domain row yet",
          {"src.tools.process_limit_bootstrap:main": "subprocess",
           "src.tools.process_tools:ProcessRuntimeManager.start_process": "subprocess",
           "src.tools.process_tools:run_owned_argv": "subprocess",
           "src.tools.process_tools:_BoundedPipeCapture.__init__": "thread",
           "src.execution.tool_package_runner:execute": "subprocess",
           "src.execution.tool_package_runner:_reap_in_owner": "background_task",
           "src.execution.tool_package_supervisor:run_isolated": "subprocess",
           "src.work_board.tool_package_native:execute": "background_task"}),
    *_gap("seraph.documents.v1", UNTRACKED_SEAM_SLICE, "document build, parse and compare have no seam or domain row yet",
          {"src.work_board.documents:DocumentService": "lifecycle_method",
           "src.work_board.documents:DocumentService.parse": "subprocess",
           "src.work_board.documents:shield_positive_cleanup": "background_task",
           "src.work_board.document_build_native:invoke": "subprocess",
           "src.work_board.document_compare_native:execute": "subprocess",
           "src.work_board.document_compare_supervisor:main": "subprocess",
           "src.work_board.document_compare_control:retry": "background_task",
           "src.work_board.document_pairs:probe_upload_profile": "subprocess"}),
    *_gap("seraph.external-adapters.v1", UNTRACKED_SEAM_SLICE,
          "MCP adapters are loaded and disconnected by the lifespan; no external-adapter seam owns them",
          {f"{_LS}mcp_manager.load_config]": "lifespan_step", f"{_PW}mcp_manager.disconnect_all]": "lifespan_step"}),
)

_BOOTSTRAP = "ADR-036 §1 composition bootstrap: "
EXCLUSIONS: tuple[Exclusion, ...] = (
    *_excluded("bootstrap", {
        "src.app:lifespan[step=boot_profile]": ("lifespan_step", _BOOTSTRAP + "validates the named profile and patch layers"),
        "src.app:lifespan[step=release_profile]": ("lifespan_step", _BOOTSTRAP + "clears the booted profile record"),
        "src.app:lifespan[step=_provider_worker_lifespan]": ("lifespan_step", _BOOTSTRAP + "enters the provider-worker host"),
        f"{_PW}_lifespan_services]": ("lifespan_step", _BOOTSTRAP + "startup/shutdown wiring of the mapped services"),
        f"{_PW}drain_tracked_tasks]": ("lifespan_step", _BOOTSTRAP + "shutdown drain of tracked tasks"),
        f"{_PW}asyncio.to_thread]": ("lifespan_step", _BOOTSTRAP + "shutdown join of tracked threads"),
        f"{_LS}run_boot_cutover]": ("lifespan_step", _BOOTSTRAP + "the #1019 boot cutover manifest step"),
        "src.api.runtime_composition:transition_domain_owner": (
            "background_task", _BOOTSTRAP + "the #1019 owner rollback/re-cutover transition"),
        "src.runtime_plugins.cli:build": ("subprocess", _BOOTSTRAP + "managed Cordis package build (manage.sh cordis build)"),
        "src.runtime_plugins.cli:reviewed_npm": ("subprocess", _BOOTSTRAP + "reviewed npm invocation for that build"),
        "src.runtime_plugins.composition:reviewed_node": ("subprocess", _BOOTSTRAP + "reviewed Node version probe"),
    }),
    *_excluded("utility", {
        "src.utils.background:track_task": ("background_task", "the tracked-task primitive every owner calls"),
        "src.utils.background:start_tracked_thread": ("thread", "the tracked-thread primitive every owner calls"),
        "src.runtime_plugins.guardian_turns:_on_loop": ("lifecycle_method", "cross-loop call helper; start is a local closure"),
        "src.runtime_plugins.guardian_turns:_on_loop.start": ("background_task", "the same helper's per-call task"),
        "src.model_fabric.execution:_run_awaitable_sync": ("thread", "sync-to-async bridge for repository coroutines"),
        "src.workflows.manager:_run_async": ("thread", "sync-to-async bridge for workflow coroutines"),
    }),
    *_excluded("test_helper", {
        "src.utils.clock:FakeClock.wait": ("background_task", "explicit-advance test clock (#1225)"),
        "src.utils.clock:FakeClock.wait_for": ("background_task", "explicit-advance test clock (#1225)"),
        "src.evals.harness:_SessionFactoryScope": ("lifecycle_method", "deterministic contract harness, not a runtime path"),
        "src.evals.harness:_eval_websocket_chat_timeout_contract._timeout_wait_for": (
            "background_task", "deterministic contract harness, not a runtime path"),
    }),
)


def rows() -> dict[str, Owner | Gap | Exclusion]:
    """Every row by key; a key that appears twice raises."""
    found: dict[str, Owner | Gap | Exclusion] = {}
    for row in (*OWNERS, *GAPS, *EXCLUSIONS):
        if row.key in found:
            raise ValueError(f"inventory key listed twice: {row.key}")
        found[row.key] = row
    return found


def accounted() -> set[tuple[str, str]]:
    return {(row.key, shape) for row in rows().values() for shape in row.shapes}


# --- The AST scan -----------------------------------------------------------------

LIFESPAN_MODULE = "src.app"
LIFESPAN_FUNCTIONS = frozenset({"lifespan", "_provider_worker_lifespan", "_lifespan_services"})
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route"})
_SUBPROCESS_ATTRS = frozenset({"Popen", "run", "call", "check_call", "check_output"})
_SPAWN_NAMES = frozenset({"create_subprocess_exec", "create_subprocess_shell", "Popen"})
_OS_SPAWN = ("fork", "forkpty", "spawn", "exec", "posix_spawn")
# Calls inside the lifespan that are plumbing, not steps: logging, paths, files.
_STEP_NOISE_PREFIXES = ("logging.", "os.", "shutil.", "settings.")
_STEP_NOISE = frozenset({"len", "sum", "dict", "list", "str", "bool", "isinstance", "Path", "RuntimeError"})


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{dotted(node.func)}()"
    if isinstance(node, ast.Await):
        return dotted(node.value)
    return type(node).__name__


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_loop_base(func: ast.AST) -> bool:
    """``asyncio.create_task``/``loop.create_task``/bare ``create_task``, not ``repository.create_task``."""
    if isinstance(func, ast.Name):
        return True
    if not isinstance(func, ast.Attribute):
        return False
    base = dotted(func.value)
    return base == "asyncio" or base.endswith(("loop", "loop()", "_loop")) or base.startswith("asyncio.get_")


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.scope: list[str] = []
        self.found: set[tuple[str, str]] = set()

    def _symbol(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _add(self, symbol: str, shape: str) -> None:
        self.found.add((f"{self.module}:{symbol}", shape))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name in {"start", "stop"}:
            # The owner of a start/stop pair is its class (or the module function itself).
            self._add(self._symbol() if self.scope else node.name, "lifecycle_method")
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                attr = decorator.func.attr
                if attr in {"websocket", "websocket_route"}:
                    self._add(".".join([*self.scope, node.name]), "websocket")
                elif attr in _HTTP_METHODS and isinstance(decorator.func.value, ast.Name) \
                        and decorator.func.value.id == "app":
                    self._add(".".join([*self.scope, node.name]), "router")
        self.scope.append(node.name)
        if self.module == LIFESPAN_MODULE and len(self.scope) == 1 and node.name in LIFESPAN_FUNCTIONS:
            self._lifespan_steps(node)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _lifespan_steps(self, function: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        calls: list[ast.AST] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Expr):
                calls.append(node.value)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                calls.append(node.value)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                calls.extend(item.context_expr for item in node.items)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                calls.append(node.iter)
        for call in calls:
            if isinstance(call, ast.Await):
                call = call.value
            if not isinstance(call, ast.Call):
                continue
            name = dotted(call.func)
            if name in _STEP_NOISE or name.startswith(_STEP_NOISE_PREFIXES) or ".getLogger" in name:
                continue
            self._add(f"{function.name}[step={name}]", "lifespan_step")

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and _callee_name(node.value.func) == "APIRouter" \
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._add(".".join([*self.scope, node.targets[0].id]), "router")
            for arg in [*node.value.args, *node.value.keywords]:
                self.visit(arg)
            return
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = {key.value: value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        if {"func", "trigger", "id"} <= set(keys) and isinstance(keys["id"], ast.Constant):
            self._add(f"{self._symbol()}[job={keys['id'].value}]", "scheduler_job")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _callee_name(func)
        base = dotted(func.value) if isinstance(func, ast.Attribute) else ""
        if name == "add_job":
            self._add(self._symbol(), "scheduler_job")
        elif name == "APIRouter":
            self._add(self._symbol(), "router")
        elif name in {"add_api_websocket_route", "add_websocket_route"}:
            self._add(self._symbol(), "websocket")
        elif name == "FastAPI":
            self._add(self._symbol(), "app_factory")
        elif name == "track_task" or (name in {"create_task", "ensure_future"} and _is_loop_base(func)):
            self._add(self._symbol(), "background_task")
        elif name == "Thread" and base in {"", "threading"}:
            self._add(self._symbol(), "thread")
        elif (name in _SPAWN_NAMES or (base == "subprocess" and name in _SUBPROCESS_ATTRS)
              or (base == "os" and name.startswith(_OS_SPAWN)) or (base == "pty" and name == "spawn")
              or (base == "multiprocessing" and name == "Process")):
            self._add(self._symbol(), "subprocess")
        self.generic_visit(node)


def scan_lifecycle_shapes(root: Path | None = None) -> set[tuple[str, str]]:
    """Every ``(module:symbol, shape)`` lifecycle finding under ``root`` (default ``backend/src``)."""
    source_root = SRC_ROOT if root is None else Path(root)
    found: set[tuple[str, str]] = set()
    for path in sorted(source_root.rglob("*.py")):
        scanner = _Scanner(module_name(path, source_root))
        scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        found |= scanner.found
    return found


def unaccounted(found: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Scan findings in no table: each one is a runtime owner outside the inventory."""
    return sorted(found - accounted())


def stale(found: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Table rows the scan no longer finds: delete or rename them."""
    return sorted(accounted() - found)


__all__ = ["EXCLUSIONS", "EXCLUSION_KINDS", "Exclusion", "GAPS", "GAP_OWNERS", "Gap", "OWNERS", "Owner",
           "PLANNED_SEAMS", "PROVIDERS", "SHAPES", "UNTRACKED_SEAM_SLICE", "accounted", "rows",
           "scan_lifecycle_shapes", "stale", "unaccounted"]
