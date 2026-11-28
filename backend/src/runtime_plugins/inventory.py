"""The source inventory: runtime lifecycle owners in ``backend/src`` and their plugin owner (#1205).

Design #1026 §5 and ADR-036 §4/§5. This module is data plus a pure AST scan.
It imports nothing from the runtime, starts nothing and changes no
composition. ``tests/test_source_inventory_1205.py`` scans ``backend/src``
for the lifecycle shapes below and fails on any finding that is in none of
the three tables, on any table row the scan no longer finds, and on any row
``row_problems`` rejects.

Coverage is the shapes the scan recognises, not every line of runtime code.
Each finding is a ``(module:symbol, shape)`` pair:

* ``lifecycle_method``: a ``def start``/``def stop`` (sync or async). The key
  is the owning class, or the module-level function itself.
* ``scheduler_job``: an ``add_job`` call, and each system-job literal (a dict
  with ``func``, ``trigger`` and ``id``) keyed ``symbol[job=<id>]``.
* ``router``: an ``APIRouter`` (keyed by the variable it is bound to), an
  ``add_api_route`` call, or a route declared straight on the FastAPI ``app``.
* ``websocket``: a ``@router.websocket`` endpoint or a websocket route call.
* ``app_factory``: a ``FastAPI(...)`` construction.
* ``background_task``: ``asyncio.create_task``/``ensure_future``, the loop and
  ``asyncio.TaskGroup`` forms, and ``track_task``. A repository's own
  ``create_task`` method is not one.
* ``thread``: ``threading.Thread``/``Timer``, ``concurrent.futures.ThreadPoolExecutor``,
  ``asyncio.to_thread`` and any ``.run_in_executor(...)``.
* ``subprocess``: exactly ``SUBPROCESS_CALLS`` (``subprocess.Popen``/``run``/
  ``call``/``check_call``/``check_output``/``getoutput``/``getstatusoutput``,
  ``asyncio.create_subprocess_exec``/``shell``, ``os.system``/``popen``,
  ``pty.spawn``/``fork``, ``multiprocessing.Process``/``Pool`` (also through
  ``get_context()``), ``concurrent.futures.ProcessPoolExecutor``), the
  ``os.fork``/``forkpty``/``spawn*``/``exec*``/``posix_spawn*`` prefixes and a
  loop's ``subprocess_exec``/``subprocess_shell``.
* ``process_hook``: ``atexit.register``, ``signal.signal`` and any
  ``.add_signal_handler(...)``.
* ``lifespan_step``: a step call inside the ``src.app`` lifespan functions,
  keyed ``function[step=<callee>]``, plus ``@app.on_event`` handlers and
  ``add_event_handler`` calls.

Names resolve through each module's imports, so ``import subprocess as sp``,
``from subprocess import run`` and ``from concurrent import futures`` reach
the same canonical name. Known limitation: a call reached only dynamically is
invisible (``getattr``, ``importlib``, ``__import__``, ``exec``/``eval``, or
a callable first stored in a variable or attribute). Review catches those.

Every finding lands in exactly one table:

* ``OWNERS``: a responsibility whose plugin owner exists today, i.e. a runtime
  domain row in ``ownership.DOMAINS``. Each row states its domain, its A3.3-1
  bundle, provider and whether it is required; ``row_problems`` checks the
  stated bundle against ``trust.DOMAIN_BUNDLES``.
* ``GAPS``: a responsibility with no plugin owner yet. It names the planned
  seam or domain, its ADR-036 §4 bundle, the tracked work expected to own it
  and why it is a gap. Gaps are listed, never hidden.
* ``EXCLUSIONS``: not a plugin by decision. Composition bootstrap (ADR-036
  §1), pure utilities, types and contracts, and test helpers. Each carries a
  one-line reason.

``ENV_FLAGS`` (#1206) classifies every composition environment flag the scan
of ``config/settings.py`` and ``manage.sh`` finds: a catalogued patch row, a
security switch that stays an environment setting, a removed env base layer,
or not composition at all.

To add a capability (for example a #1308 child): give its new lifecycle
shapes rows here. A new domain needs its row in ``ownership.DOMAINS`` and
``trust.DOMAIN_BUNDLES`` first; until then it is a gap.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SRC_ROOT = Path(__file__).resolve().parents[1]

SHAPES = frozenset({"lifecycle_method", "scheduler_job", "router", "websocket", "app_factory", "background_task",
                    "thread", "subprocess", "lifespan_step", "process_hook"})
PROVIDERS = frozenset({"python", "node"})
EXCLUSION_KINDS = frozenset({"bootstrap", "utility", "contract", "test_helper"})

# Planned seams and domains with no runtime domain row yet (design #1026 §2,
# ADR-036 §4), with their bundle. ``None`` is the composition bootstrap itself (ADR-036 §1).
PLANNED_SEAMS: Mapping[str | None, str | None] = MappingProxyType({
    "seraph.settings.v1": "seraph-cockpit", "seraph.http.v1": "seraph-cockpit",
    "seraph.websocket.v1": "seraph-cockpit", "seraph.observer.v1": "seraph-edge",
    "seraph.channels.v1": "seraph-channels", "seraph.browser.v1": "seraph-capabilities",
    "seraph.repository.v1": "seraph-capabilities", "seraph.sandbox.v1": "seraph-capabilities",
    "seraph.documents.v1": "seraph-capabilities", "seraph.external-adapters.v1": "seraph-capabilities",
    "seraph.mail.v1": "seraph-capabilities", "seraph.calendar.v1": "seraph-capabilities",
    "seraph.github.v1": "seraph-capabilities",
    None: None,
})
# The tracked issue expected to own a gap: an A3.3 slice (#1026 slicing plan)
# or a capability seam slice (ADR-036 §5). Every gap cites one of these; a new
# gap needs its issue filed and listed here first.
GAP_OWNERS = MappingProxyType({
    "#1208": "A3.3-5 interface domains and plugins (settings, http, websocket)",
    "#1209": "A3.3-6 observer and channel interface plugins",
    "#1211": "A3.3-8 retire the self-starting legacy composition (after the #1210 Node entry)",
    "#1327": "browser capability seam",
    "#1328": "repository capability seam",
    "#1329": "sandbox, process and tool-package capability seam",
    "#1330": "documents capability seam",
    "#1331": "MCP external-adapter seam",
    "#1333": "mail capability seam",
    "#1334": "calendar capability seam",
    "#1335": "GitHub capability seam",
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


def _owned(domain: str, bundle: str, rows: dict[str, str], *, required: bool = False,
           provider: str = "python") -> tuple[Owner, ...]:
    return tuple(Owner(key, _shapes(shapes), domain, bundle, provider, required) for key, shapes in rows.items())


def _gap(target: str | None, bundle: str | None, owner: str, reason: str, rows: dict[str, str], *,
         required: bool = False, provider: str = "python") -> tuple[Gap, ...]:
    return tuple(Gap(key, _shapes(shapes), target, bundle, provider, required, owner, reason)
                 for key, shapes in rows.items())


def _excluded(kind: str, rows: dict[str, tuple[str, str]]) -> tuple[Exclusion, ...]:
    return tuple(Exclusion(key, _shapes(shapes), kind, reason) for key, (shapes, reason) in rows.items())


_LS = "src.app:_lifespan_services[step="
_PW = "src.app:_provider_worker_lifespan[step="
_JOB = "src.scheduler.engine:init_scheduler[job="
_T = "seraph-trust"

OWNERS: tuple[Owner, ...] = (
    # seraph-trust: pinned, required (ADR-036 §3).
    *_owned("seraph.state.v1", _T, {
        f"{_PW}attach]": "lifespan_step", f"{_PW}worker.acquire_workspace]": "lifespan_step",
        f"{_PW}worker.activate]": "lifespan_step", f"{_PW}worker.close_gate]": "lifespan_step",
        f"{_PW}worker.wait_maintenance]": "lifespan_step", f"{_PW}worker.begin_shutdown]": "lifespan_step",
        # The worker-owned backup task (#1024 in-process maintenance handoff).
        "src.runtime_plugins.worker:ProviderWorker.start_backup": "background_task",
        f"{_PW}worker.close_state]": "lifespan_step", f"{_PW}worker.release_workspace]": "lifespan_step",
        f"{_PW}worker.detach]": "lifespan_step",
        # #1024 in-process maintenance handoff: the backup step runs on one executor thread.
        "src.runtime_plugins.worker:ProviderWorker.maintenance_backup": "thread",
    }, required=True),
    *_owned("seraph.authority.v1", _T, {
        f"{_LS}_reconcile_operator_auth_mode]": "lifespan_step",
        "src.auth.service:verify_secret": "thread",
    }, required=True),
    *_owned("seraph.audit.v1", _T, {
        "src.audit.runtime:log_background_task_event_sync": "background_task",
        "src.audit.runtime:log_integration_event": "background_task",
        "src.audit.runtime:log_integration_event_sync": "background_task",
        "src.llm_runtime:_log_llm_runtime_event_sync": "background_task",
    }, required=True),
    *_owned("seraph.tasks.v1", _T, {
        f"{_LS}durable_job_repository.recover_stale_jobs]": "lifespan_step",
        f"{_LS}current_task_service]": "lifespan_step",
        "src.work_board.general_task:GeneralTaskService": "lifecycle_method",
        "src.work_board.general_task:GeneralTaskService.retain_native_invocation.closed": "background_task",
    }, required=True),
    *_owned("seraph.inference.v1", _T, {
        f"{_LS}hydrate_openrouter_credential]": "lifespan_step",
        f"{_LS}run_openrouter_route_revoke_at_boot]": "lifespan_step",
        f"{_LS}ModelFabricRepository().cleanup_settled_audio_documentation]": "lifespan_step",
        f"{_LS}init_llm_logging]": "lifespan_step",
        f"{_LS}near_tee_catalog_refresher.start]": "lifespan_step",
        f"{_LS}near_tee_catalog_refresher.stop]": "lifespan_step",
        "src.model_fabric.near_tee_catalog:NearTeeCatalogRefresher": "lifecycle_method",
        "src.model_fabric.near_tee_catalog:NearTeeCatalogRefresher.start": "background_task",
        "src.model_fabric.accounting:DurableInferenceBrokerMixin._before_release": "background_task",
        "src.model_fabric.gpu_admission:GpuAdmissionBroker._schedule_deadline_watchdog_locked": "thread",
        "src.llm_runtime:completion_with_fallback": "thread",
        "src.workflows.inference_accounting:InferenceAccountingRepositoryMixin.verify_inference_ledger_in_background":
            "thread",
    }, required=True),
    # seraph-agent.
    *_owned("seraph.agent-loop.v1", "seraph-agent", {"src.agent.direct_chat:run_direct_local_chat": "thread"}),
    *_owned("seraph.guardian.v1", "seraph-agent", {
        f"{_LS}current_goal_discovery]": "lifespan_step",
        "src.guardian.goal_discovery:GoalDiscoveryService": "lifecycle_method",
        "src.guardian.opportunity_runtime:run_opportunity_tick": "background_task",
    }),
    *_owned("seraph.capabilities.v1", "seraph-agent", {
        f"{_LS}default_manifest_roots_for_workspace]": "lifespan_step",
        f"{_LS}skill_manager.init]": "lifespan_step", f"{_LS}runbook_manager.init]": "lifespan_step",
        f"{_LS}workflow_manager.init]": "lifespan_step", f"{_LS}starter_pack_manager.init]": "lifespan_step",
        f"{_LS}routine_service.recover_pending_installs]": "lifespan_step",
        "src.native_tools.task_adapters:ToolRegistry": "lifecycle_method",
        "src.native_tools.task_adapters:ToolRegistry.begin_invocation": "background_task thread",
        "src.runtime_plugins.governed_services:_invoke_capability": "thread",
    }),
    # seraph-capabilities.
    *_owned("seraph.research.v1", "seraph-capabilities",
            {"src.workflows.research_coordinator:_continue_parent": "background_task"}),
    # seraph-domains.
    *_owned("seraph.goals.v1", "seraph-domains", {
        f"{_LS}goal_programme_service.start]": "lifespan_step",
        f"{_LS}goal_programme_service.stop]": "lifespan_step",
        "src.guardian.goal_programmes:GoalProgrammeService": "lifecycle_method",
        "src.guardian.goal_snapshot_to_file:GoalSnapshotToFileAdapter._invoke_workflow": "thread",
    }),
    *_owned("seraph.memory.v1", "seraph-domains", {
        f"{_LS}ensure_soul_exists]": "lifespan_step",
        f"{_LS}current_method.start]": "lifespan_step", f"{_LS}current_method.stop]": "lifespan_step",
        f"{_LS}historical_method_service.start]": "lifespan_step",
        f"{_LS}historical_method_service.stop]": "lifespan_step",
        "src.memory.task_methods:CurrentMethod": "lifecycle_method",
        "src.memory.task_methods:CurrentMethod.start": "thread",
        "src.memory.task_methods:_inspect_method": "thread",
        "src.memory.task_methods:_stage": "thread",
        "src.work_board.historical_method:HistoricalMethodService": "lifecycle_method",
        "src.memory.near_embedding_reindex:start_reindex": "background_task",
        "src.memory.near_embedding_reindex:_run": "thread",
        "src.memory.task_lessons:create_task_lesson": "background_task thread",
        "src.memory.task_lessons:create_task_lesson.completed": "background_task",
        "src.memory.task_lessons:_recover_ended_io": "thread",
        "src.memory.task_lessons:_repair_lesson_mirror": "thread",
        "src.memory.task_lessons:inspect_task_lesson": "thread",
        "src.memory.decay:apply_memory_decay_policies": "thread",
        "src.memory.hybrid_retrieval:_retrieve_hybrid_memory": "thread",
        "src.memory.pipeline.merge:_write_embedding": "thread",
    }),
    *_owned("seraph.artifacts.v1", "seraph-domains",
            {f"{_LS}cleanup_retired_audio_quarantine_at_startup]": "lifespan_step"}),
    *_owned("seraph.conversation.v1", "seraph-domains", {
        f"{_LS}TaskContinuityService]": "lifespan_step",
        f"{_LS}continuity.start]": "lifespan_step", f"{_LS}continuity.stop]": "lifespan_step",
        f"{_LS}session_manager.bind_task_continuity]": "lifespan_step",
        "src.conversation.task_context:TaskContinuityService": "lifecycle_method",
        "src.agent.session:SessionManager.get_history_text": "thread",
    }),
    *_owned("seraph.connections.v1", "seraph-domains", {
        f"{_LS}ConnectionSyncService]": "lifespan_step",
        f"{_LS}connection_sync_runtime.start]": "lifespan_step",
        f"{_LS}connection_sync_runtime.stop]": "lifespan_step",
        f"{_LS}purge_cordis_mail_results]": "lifespan_step",
        "src.integrations.connection_sync:ConnectionSyncService": "lifecycle_method",
        "src.integrations.connection_sync:ConnectionSyncService.synchronize": "background_task",
        "src.integrations.connection_sync:ConnectionSyncService._synchronize": "thread",
        "src.integrations.connection_sync:ConnectionSyncService._synchronize.fetch": "background_task",
        # Retention-exempt timer (lifecycle.RETENTION_JOBS) over Cordis mail results.
        f"{_JOB}cordis_mail_retention]": "scheduler_job",
    }),
    # seraph-lifecycle. Each gated system job maps to the domain lifecycle.job_domain gives it.
    *_owned("seraph.scheduler.v1", "seraph-lifecycle", {
        f"{_LS}init_scheduler]": "lifespan_step", f"{_LS}sync_scheduled_jobs]": "lifespan_step",
        f"{_LS}shutdown_scheduler]": "lifespan_step",
        f"{_LS}runtime_lifecycle.start]": "lifespan_step", f"{_LS}runtime_lifecycle.stop]": "lifespan_step",
        "src.runtime_plugins.lifecycle:RuntimeLifecycle": "lifecycle_method",
        "src.scheduler.engine:init_scheduler": "scheduler_job",
        "src.scheduler.engine:sync_scheduled_jobs": "scheduler_job",
        # The EVENT_JOB_MISSED listener's durable status write (#1025 D2), one tracked task per dropped fire.
        "src.scheduler.engine:_record_missed_fire": "background_task",
        **{f"{_JOB}{job}]": "scheduler_job" for job in (
            "programme_daily_digest", "goal_public_discovery", "memory_consolidation", "goal_check",
            "calendar_scan", "strategist_tick", "guardian_opportunity_assessment", "daily_briefing",
            "evening_review", "activity_digest", "end_of_day_goal_report", "weekly_activity_review",
            "screenshot_folder_ingest", "screenshot_folder_analysis", "screenshot_observation_digest",
            "inference_ledger_verification")},
    }),
    *_owned("seraph.dispatch.v1", "seraph-lifecycle", {
        f"{_JOB}work_board_dispatch]": "scheduler_job",
        "src.work_board.dispatcher:_await_bounded_model_call": "background_task",
        "src.work_board.dispatcher:_await_communication_model_call": "background_task",
        "src.work_board.dispatcher:WorkBoardDispatcher._capability_preflight": "thread",
        "src.work_board.dispatcher:WorkBoardDispatcher._execute_direct_adapter": "thread",
        "src.work_board.dispatcher:WorkBoardDispatcher._execute_direct_adapter.model_call": "thread",
        "src.work_board.dispatcher:_reserve_repo_repair_execution_capacity": "thread",
        "src.work_board.dispatcher:_settle_repo_repair_execution_capacity": "thread",
        "src.work_board.communication_preparation:_run_source": "background_task",
        "src.work_board.communication_preparation:_source_output": "thread",
        "src.work_board.communication_preparation:cleanup_plan": "thread",
        "src.work_board.communication_preparation:invoke": "thread",
        "src.work_board.communication_preparation:read_plan": "thread",
        "src.work_board.communication_preparation:stage_plan_authority": "thread",
    }),
)

_HTTP_ROUTERS = (
    "activity", "approvals", "audit", "auth", "automation", "browser", "calendar", "calendar_reschedule", "canvas",
    "capabilities", "capability_packs", "catalog", "chat", "documents", "evolution", "extensions", "forgejo",
    "goals", "guardian_inbox", "mail", "mcp", "memory", "moltbook", "nodes", "operator", "reports", "sessions",
    "skills", "task_evidence", "tools", "work_board", "workflows",
)
_REPORT_REASON = "operator report route spawns the strategy-claim scan; it is served only through the cockpit API"
_COCKPIT = "seraph-cockpit"
_CAP = "seraph-capabilities"

GAPS: tuple[Gap, ...] = (
    *_gap("seraph.settings.v1", _COCKPIT, "#1208",
          "settings, status, composition and configuration repair have no interface domain yet",
          {"src.api.settings:router": "router", "src.api.model_fabric_settings:router": "router",
           "src.api.profile:router": "router", "src.api.vault:router": "router",
           "src.api.runtime_composition:router": "router", "src.app:create_app.runtime_status": "router",
           "src.api.runtime_composition:transition_domain_owner": "background_task",
           "src.api.settings:_choose_screenshot_folder_with_native_dialog": "subprocess",
           "src.api.settings:_local_runtime_profile_proof_summary_fast": "thread",
           "src.api.settings:_report_receipt_summary_fast": "thread",
           "src.api.settings:_screenshot_folder_summary_fast": "thread",
           "src.api.settings:get_repo_sandbox_settings": "thread",
           f"{_LS}get_or_create_profile]": "lifespan_step"},
          required=True),
    *_gap("seraph.http.v1", _COCKPIT, "#1208",
          "HTTP routes and their request-scoped work run under create_app, not an http-interface plugin",
          {"src.app:create_app": "app_factory", "src.app:create_app.health": "router",
           "src.api.router:api_router": "router",
           **{f"src.api.{name}:router": "router" for name in _HTTP_ROUTERS},
           "src.extensions.github_followthrough:github_followthrough_router": "router",
           "src.guardian.source_watch:source_watch_router": "router",
           "src.integrations.github_activity:github_activity_router": "router",
           "src.integrations.project_source:project_source_router": "router",
           "src.workflows.repo_publication:router": "router",
           "src.workflows.routines:routine_router": "router",
           # REST chat: the agent turn thread, the session-memory flush and the
           # revocation watcher, like their WebSocket equivalents.
           "src.api.chat:chat": "background_task thread",
           "src.api.chat:_begin_rest_revocation_watch": "background_task",
           "src.api.evolution:_run_evolution_thread_cancel_safe": "background_task thread",
           "src.api.activity:get_activity_ledger": "thread",
           "src.api.capabilities:get_capability_overview": "thread",
           "src.api.extension_metadata:bounded_extension_metadata": "thread",
           **{f"src.api.operator:{name}": "thread" for name in (
               "get_operator_background_sessions", "get_operator_benchmark_proof", "get_operator_control_plane",
               "get_operator_m5_operating_layer", "get_operator_m7_cockpit")}}),
    *_gap("seraph.http.v1", _COCKPIT, "#1208",
          "the cockpit home projection starts from the lifespan; no interface domain owns it",
          {"src.operator.home_projection:HomeProjection": "lifecycle_method",
           f"{_LS}home_projection.start]": "lifespan_step", f"{_LS}home_projection.stop]": "lifespan_step"}),
    *_gap("seraph.http.v1", _COCKPIT, "#1208", _REPORT_REASON,
          {"src.cockpit.post_dp_operator_debugging_recovery:_strategy_claim_scan_command_evidence": "subprocess",
           "src.extensions.post_dp_browser_computer_use_reliability:_run_strategy_claim_scan": "subprocess",
           "src.extensions.post_dp_marketplace_lifecycle_gap_closure:_run_strategy_claim_scan": "subprocess",
           "src.guardian.post_dp_guardian_memory_gap_closure:guardian_memory_false_claim_scan_v2_receipts":
               "subprocess"}),
    *_gap("seraph.websocket.v1", _COCKPIT, "#1208",
          "WebSocket endpoints and their agent-turn worker run under create_app; no websocket-interface plugin",
          {"src.api.ws:router": "router", "src.api.ws:websocket_chat": "websocket background_task thread",
           "src.api.ws:websocket_work_board_events": "websocket background_task",
           "src.api.ws:_authorized_with_timeout": "background_task",
           "src.api.ws:_await_authorized": "background_task"}),
    *_gap("seraph.observer.v1", "seraph-edge", "#1209",
          "observer and edge-context adapters run without a consent-scoped observer plugin",
          {"src.api.observer:router": "router", "src.api.selected_context:router": "router",
           "src.observer.manager:ContextManager.refresh": "background_task",
           "src.observer.local_capture_upload:_enforce_store_cap": "thread",
           "src.observer.local_capture_upload:ingest_local_capture": "thread",
           "src.observer.screenshot_folder_source:build_image_observation": "thread",
           "src.observer.screenshot_folder_source:scan_screenshot_folder": "thread",
           "src.observer.screenshot_semantic_analysis:_analyze_with_near_tee": "thread",
           "src.observer.sources.calendar_source:gather_calendar": "thread",
           "src.scheduler.jobs.screenshot_folder_analysis:run_screenshot_folder_analysis": "background_task",
           "src.scheduler.jobs.screen_cleanup:_cleanup_capture_uploads": "thread",
           "src.workflows.selected_context_runtime:upload": "background_task thread",
           "src.workflows.selected_context_runtime:discard": "thread",
           "src.workflows.selected_context_runtime:inspect": "thread",
           f"{_JOB}screen_cleanup]": "scheduler_job",
           f"{_LS}context_manager.refresh]": "lifespan_step",
           **{f"{_LS}context_manager.update_{mode}_mode]": "lifespan_step"
              for mode in ("interruption", "capture", "tool_policy", "mcp_policy", "approval")}}),
    *_gap("seraph.channels.v1", "seraph-channels", "#1209",
          "paired channels are plain routes; no channel-interface plugin activates them",
          {"src.api.telegram:router": "router"}),
    *_gap(None, None, "#1211",
          "Python spawns the Cordis child from the lifespan; #1210 inverts the parent and #1211 removes this host",
          {"src.runtime_plugins.bridge:CordisHost": "lifecycle_method",
           "src.runtime_plugins.bridge:CordisHost.start": "subprocess",
           "src.runtime_plugins.bridge:CordisHost._task": "background_task",
           f"{_LS}cordis_host.start]": "lifespan_step", f"{_LS}cordis_host.stop]": "lifespan_step"}),
    *_gap("seraph.browser.v1", _CAP, "#1327", "browser execution has no seam or domain row yet",
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
           "src.browser.pinned_transport:<module>": "thread",
           "src.tools.browser_tool:browse_webpage": "thread",
           "src.api.browser:_capture_or_raise": "thread",
           f"{_LS}profiled_interaction_sessions.start]": "lifespan_step",
           f"{_LS}profiled_interaction_sessions.stop]": "lifespan_step"}),
    *_gap("seraph.repository.v1", _CAP, "#1328", "repository repair executors have no seam or domain row yet",
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
           "src.execution.repo_supervisor:enable_subreaper": "process_hook",
           "src.execution.repo_supervisor:main": "process_hook",
           "src.execution.repo_worker:_run_fixed": "subprocess thread",
           "src.api.workflows:_repo_change_track_sandbox_call": "background_task thread",
           "src.api.workflows:_recover_repo_change_after_restart_inner": "thread",
           "src.api.workflows:_repo_change_patch_read_blocked": "thread",
           "src.api.workflows:_resolve_repo_repair_proposal": "thread",
           "src.api.workflows:cancel_repo_change_for_authenticated_operator": "thread",
           "src.workflows.repo_repair_source:execute_repository_iteration": "background_task thread",
           "src.workflows.repo_repair_source:grant_repository_iteration_consent": "background_task thread",
           "src.workflows.repo_repair_source:run_repository_iteration": "background_task thread",
           "src.workflows.repo_repair_source:prepare_repository_native_source": "thread",
           "src.workflows.repo_repair_source:_prepare_repository_iteration": "thread",
           "src.workflows.repo_repair_stop:stop_repository_root": "thread",
           "src.workflows.repo_repair:RepoRepairService._recheck_generation_authority": "thread",
           "src.workflows.repo_repair:RepoRepairService.generate_proposal": "thread",
           "src.workflows.repo_repair:RepoRepairService.resolve_proposal": "thread",
           "src.workflows.repo_publication:RepoPublicationService.execute": "thread",
           "src.workflows.repo_publication:RepoPublicationService.tested_runtime_current": "thread",
           "src.workflows.native_software_engineering:run_native_software_engineering_fixture": "thread"}),
    *_gap("seraph.sandbox.v1", _CAP, "#1329", "process and tool-package execution has no seam or domain row yet",
          {"src.tools.process_limit_bootstrap:main": "subprocess process_hook",
           "src.tools.process_tools:ProcessRuntimeManager.start_process": "subprocess",
           "src.tools.process_tools:run_owned_argv": "subprocess",
           "src.tools.process_tools:_BoundedPipeCapture.__init__": "thread",
           "src.execution.tool_package_runner:execute": "subprocess",
           "src.execution.tool_package_runner:_reap_in_owner": "background_task thread",
           "src.execution.tool_package_supervisor:run_isolated": "subprocess",
           "src.execution.tool_package_supervisor:main": "process_hook",
           "src.work_board.tool_package_native:execute": "background_task"}),
    *_gap("seraph.documents.v1", _CAP, "#1330", "document build, parse and compare have no seam or domain row yet",
          {"src.work_board.documents:DocumentService": "lifecycle_method",
           "src.work_board.documents:DocumentService.parse": "subprocess",
           "src.work_board.documents:shield_positive_cleanup": "background_task",
           "src.work_board.document_build_native:invoke": "subprocess",
           "src.work_board.document_compare_native:execute": "subprocess",
           "src.work_board.document_compare_supervisor:main": "subprocess process_hook",
           "src.work_board.document_compare_child:host_limits": "process_hook",
           "src.work_board.document_read_child:confine": "process_hook",
           "src.work_board.document_compare_control:retry": "background_task",
           "src.work_board.document_pairs:probe_upload_profile": "subprocess"}),
    *_gap("seraph.external-adapters.v1", _CAP, "#1331",
          "MCP adapters are loaded and disconnected by the lifespan; no external-adapter seam owns them",
          {f"{_LS}mcp_manager.load_config]": "lifespan_step", f"{_PW}mcp_manager.disconnect_all]": "lifespan_step",
           "src.tools.mcp_manager:MCPManager._run_async": "thread"}),
    # ADR-036 §4 puts mail, calendar and GitHub in seraph-capabilities; these
    # owned executors are not the connections domain's credential references.
    *_gap("seraph.mail.v1", _CAP, "#1333", "mail read/reply execution has no seam or domain row yet",
          {**{f"src.integrations.mail_reply_runtime:{name}": "thread" for name in (
               "actual_source", "execute", "observe", "paired_account", "reserve_private", "snapshot")},
           "src.integrations.mail_reply_runtime:run_owned": "background_task",
           "src.integrations.gmail_controls:run_mail_source_control": "thread"}),
    *_gap("seraph.calendar.v1", _CAP, "#1334", "calendar reschedule execution has no seam or domain row yet",
          {**{f"src.integrations.calendar_reschedule_runtime:{name}": "thread" for name in (
               "create_consent", "execute", "observe", "paired_proof", "reserve_private", "snapshot")},
           "src.integrations.calendar_reschedule_runtime:run_owned": "background_task",
           "src.integrations.calendar_controls:_run_control": "thread"}),
    *_gap("seraph.github.v1", _CAP, "#1335", "the GitHub CLI reader has no seam or domain row yet",
          {"src.integrations.github_cli:GhCliReader._run": "background_task thread",
           "src.integrations.github_cli:GhCliReader.alocate": "thread"}),
)

_BOOTSTRAP = "ADR-036 §1 composition bootstrap: "
_BRIDGE = "per-call ThreadPoolExecutor(max_workers=1) bridge from a sync tool host to asyncio.run"
EXCLUSIONS: tuple[Exclusion, ...] = (
    *_excluded("bootstrap", {
        "src.app:lifespan[step=boot_profile]": ("lifespan_step", _BOOTSTRAP + "validates the named profile and patch layers"),
        "src.app:lifespan[step=release_profile]": ("lifespan_step", _BOOTSTRAP + "clears the booted profile record"),
        "src.app:lifespan[step=_provider_worker_lifespan]": ("lifespan_step", _BOOTSTRAP + "enters the provider-worker host"),
        f"{_PW}_lifespan_services]": ("lifespan_step", _BOOTSTRAP + "startup/shutdown wiring of the mapped services"),
        f"{_PW}drain_tracked_tasks]": ("lifespan_step", _BOOTSTRAP + "shutdown drain of tracked tasks"),
        f"{_PW}asyncio.to_thread]": ("lifespan_step", _BOOTSTRAP + "shutdown join of tracked threads"),
        "src.app:_provider_worker_lifespan": ("thread", _BOOTSTRAP + "that shutdown join runs on one to_thread call"),
        f"{_LS}run_boot_cutover]": ("lifespan_step", _BOOTSTRAP + "the #1019 boot cutover manifest step"),
        f"{_LS}record_env_base_layer]": ("lifespan_step", _BOOTSTRAP + "audits deprecated composition env flags (#1206)"),
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
        "src.security.http_transport:default_resolver": ("thread", "blocking getaddrinfo offload for the egress resolver"),
        **{f"src.tools.{module}:_run": ("thread", _BRIDGE) for module in (
            "goal_tools", "guardian_routine_tools", "scheduled_job_tools", "session_search_tool", "todo_tool",
            "vault_tools", "work_board_tools")},
    }),
    *_excluded("test_helper", {
        "src.utils.clock:FakeClock.wait": ("background_task", "explicit-advance test clock (#1225)"),
        "src.utils.clock:FakeClock.wait_for": ("background_task", "explicit-advance test clock (#1225)"),
        "src.evals.harness:_SessionFactoryScope": ("lifecycle_method", "deterministic contract harness, not a runtime path"),
        "src.evals.harness:_eval_websocket_chat_timeout_contract._timeout_wait_for": (
            "background_task", "deterministic contract harness, not a runtime path"),
        "src.evals.harness:_browse_webpage_async": ("thread", "deterministic contract harness, not a runtime path"),
        "src.evals.harness:_run_governed_completion_fixture": (
            "thread", "deterministic contract harness, not a runtime path"),
    }),
)


# --- Composition environment flags (#1206, ADR-036 §6) --------------------------
#
# Three scans feed this table, and ``tests/test_composition_env_flags_1206.py``
# fails on any unclassified or stale name:
# * every boolean settings field in ``config/settings.py`` (``bool``,
#   ``StrictBool``, ``Optional``/``Union``/``bool | None`` and ``Annotated``
#   wrappers, and nested settings models joined by ``__``);
# * every ``$NAME_ENABLED`` or ``${NAME_ENABLED...}`` reference in ``manage.sh``;
# * every environment variable ``backend/src`` reads directly
#   (``os.environ.get``/``os.getenv``/``os.environ[...]``/``in os.environ`` and
#   the ``ENV_READER_FUNCS`` helpers, at their name argument), with module string
#   constants and their imports resolved. Aliases of the environment mapping
#   are best-effort: ``import os as o``, ``from os import environ as E`` /
#   ``getenv as G`` and a simple ``env = os.environ`` assignment are tracked;
#   a conditional or computed alias (``values = env or os.environ``) is not,
#   and review catches those. A flag it finds belongs in ``ENV_FLAGS``; any other name
#   (credential, path, platform, server process, test) is in ``ENV_READS``. A read
#   whose name is computed at runtime is listed in ``DYNAMIC_ENV_READS``.
#
# ``STRING_SELECTORS`` is hand-kept and review-only: no scan finds a string
# settings field that selects composition, so review must add each one.
#
# * ``row``: selects composition, so it has a catalogued patch row in
#   ``profiles.PATCH_ROWS`` (typed schema, env base stage, removal issue). ``gates``
#   lists the inventory keys whose activation it decides.
# * ``security_switch``: stays a startup-validated environment setting (§6):
#   authentication, egress, consent, privacy, execution, or a setting of a
#   pinned trust service. ``profiles.SECURITY_SETTINGS`` names it, so a patch
#   row naming it is refused as a security setting.
# * ``removed``: the env base layer is retired (its row's ``env_stage``); the
#   flag is still listed so an install that sets it is handled, not ignored.
# * ``not_composition``: diagnostics that select no plugin, provider or job.

ENV_FLAG_KINDS = frozenset({"row", "security_switch", "removed", "not_composition"})
SETTINGS_FILE = SRC_ROOT.parent / "config" / "settings.py"
LAUNCHER_FILE = SRC_ROOT.parents[1] / "manage.sh"


@dataclass(frozen=True)
class EnvFlag:
    env_name: str
    # The settings attribute (nested fields joined by ``__``); None for a
    # flag only the managed launcher reads.
    setting: str | None
    kind: str
    row_id: str | None
    gates: tuple[str, ...]
    reason: str


def _flag(env_name: str, kind: str, reason: str, *, row_id: str | None = None, gates: tuple[str, ...] = (),
          launcher: bool = False) -> EnvFlag:
    return EnvFlag(env_name, None if launcher else env_name.lower(), kind, row_id, gates, reason)


_EGRESS = "egress authority: "
_CONSENT = "consent: "
_PINNED_INFERENCE = "a setting of the pinned seraph.inference.v1 trust service (ADR-036 §3): "
ENV_FLAGS: tuple[EnvFlag, ...] = (
    _flag("SCHEDULER_ENABLED", "row", "selects whether the scheduler and its lifecycle domain start",
          row_id="seraph.scheduler.v1#enabled",
          gates=(f"{_LS}init_scheduler]", f"{_LS}runtime_lifecycle.start]")),
    _flag("USE_DELEGATION", "row", "selects the orchestrator and specialists instead of the single chat agent",
          row_id="seraph.delegation.v1#enabled"),
    _flag("SCREENSHOT_FOLDER_INGEST_ENABLED", "row", "selects the screenshot-folder ingest and analysis jobs",
          row_id="seraph.scheduler.v1#screenshot_folder_ingest",
          gates=(f"{_JOB}screenshot_folder_ingest]", f"{_JOB}screenshot_folder_analysis]")),
    _flag("SCREENSHOT_OBSERVATION_DIGEST_ENABLED", "row", "selects the screenshot observation digest job",
          row_id="seraph.scheduler.v1#screenshot_observation_digest",
          gates=(f"{_JOB}screenshot_observation_digest]",)),
    _flag("END_OF_DAY_REPORT_ENABLED", "row", "selects the stored end-of-day goal report job",
          row_id="seraph.scheduler.v1#end_of_day_report", gates=(f"{_JOB}end_of_day_goal_report]",)),
    _flag("OPERATOR_AUTH_LOCAL_NO_LOGIN", "security_switch", "operator authentication (#1161), startup-validated"),
    _flag("OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS", "security_switch", "operator authentication test bypass"),
    _flag("OPERATOR_AUTH_COOKIE_SECURE", "security_switch", "operator session cookie transport security"),
    _flag("OPENROUTER_PROVIDER_ONLY", "security_switch", _PINNED_INFERENCE + "retired-provider routing policy"),
    _flag("OPENROUTER_ALLOW_FALLBACKS", "security_switch", _PINNED_INFERENCE + "retired-provider routing policy"),
    _flag("OPENROUTER_REQUIRE_PARAMETERS", "security_switch", _PINNED_INFERENCE + "retired-provider routing policy"),
    _flag("OPENROUTER_ZERO_DATA_RETENTION", "security_switch", _PINNED_INFERENCE + "provider data retention"),
    _flag("NEAR_TEE_CATALOG_REFRESH_ENABLED", "security_switch",
          _PINNED_INFERENCE + "egress to NEAR's public catalog",
          gates=(f"{_LS}near_tee_catalog_refresher.start]",)),
    _flag("LLM_LOG_ENABLED", "security_switch", _PINNED_INFERENCE + "local records of inference calls",
          gates=(f"{_LS}init_llm_logging]",)),
    _flag("LLM_LOG_CONTENT", "security_switch", "privacy: writes prompts and responses to the local call log"),
    _flag("SCREEN_DERIVED_LLM_ALLOW_REMOTE", "security_switch",
          _CONSENT + "whether screen-derived content may reach a remote model"),
    _flag("SCREEN_DERIVED_LLM_REQUIRE_PROFILE_PROOF", "security_switch",
          _CONSENT + "the proof a screen-derived prompt needs before dispatch"),
    _flag("END_OF_DAY_REPORT_LLM_ENABLED", "security_switch",
          _CONSENT + "whether the end-of-day report sends screen-derived content to inference"),
    _flag("EMAIL_REPORTS_ENABLED", "security_switch", _EGRESS + "sends reports by email"),
    _flag("EMAIL_REPORTS_PREVIEW_REQUIRED", "security_switch", "approval gate on report email egress"),
    _flag("SMTP_USE_TLS", "security_switch", "mail transport security"),
    _flag("REPO_SANDBOX__ENABLED", "security_switch", "execution authority: the governed repository executor"),
    _flag("REPO_SANDBOX__REQUIRE_ISOLATION", "security_switch", "execution isolation for repository commands"),
    # String selectors (not found by the boolean scan; listed by review).
    _flag("SCREEN_ANALYSIS_PROVIDER", "security_switch",
          _PINNED_INFERENCE + "selects the screenshot analysis provider, behind its consent"),
    _flag("SERAPH_VLM_MODE", "security_switch", _PINNED_INFERENCE + "selects the retired local VLM route"),
    # Launcher-only flags (manage.sh starts a native process before the backend boots).
    _flag("DAEMON_ENABLED", "security_switch",
          _CONSENT + "starts the native screen-capture daemon under the OS capture permission", launcher=True),
    _flag("PROXY_ENABLED", "security_switch",
          "execution authority: starts the stdio proxy that exposes local MCP servers", launcher=True),
    # Read straight from the process environment by the observer (not settings fields).
    _flag("SERAPH_SCREEN_ANALYSIS_PROVIDER", "security_switch",
          _PINNED_INFERENCE + "alias of SCREEN_ANALYSIS_PROVIDER read by the observer", launcher=True),
    _flag("SERAPH_SCREEN_ANALYSIS_ENABLED", "security_switch",
          _CONSENT + "default for whether screen analysis runs", launcher=True),
    _flag("SERAPH_PRESERVE_SCREEN_CAPTURES", "security_switch",
          "privacy: default for whether screen capture images are preserved", launcher=True),
    _flag("TRACE_WAS_ENABLED", "not_composition",
          "manage.sh's own xtrace bookkeeping variable; never read from the env file", launcher=True),
    _flag("DEBUG", "not_composition", "diagnostics only; selects no plugin, provider or job"),
    _flag("DATABASE_ECHO", "not_composition", "SQL echo diagnostics only; selects no plugin, provider or job"),
)
# String settings fields that select composition. Hand-kept and review-only:
# no scan finds these, so review adds each new selector here.
STRING_SELECTORS = frozenset({"SCREEN_ANALYSIS_PROVIDER", "SERAPH_VLM_MODE"})
_LAUNCHER_FLAG = re.compile(r"\$\{?([A-Z][A-Z0-9_]*_ENABLED)\b")
_ENV = "src.observer.screen_analysis_settings"

# Environment variables ``backend/src`` reads directly that are not flags.
ENV_READ_KINDS = frozenset({"credential", "path", "platform", "server_process", "test"})
ENV_READS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "OPENROUTER_API_KEY": ("credential", "retired provider key, read only to redact and hydrate the vault"),
    "SERAPH_DAEMON_STATUS_FILE": ("path", "where the native daemon writes its status"),
    "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR": ("path", "screen capture archive root"),
    "SERAPH_SCREENSHOT_FOLDER": ("path", "the operator's screenshot folder"),
    "SERAPH_WORKSPACE_LIFECYCLE_PATH": ("path", "production workspace lifecycle receipt path"),
    "BACKEND_DATA_PATH_PROD": ("path", "the production host workspace bind path"),
    "WORKSPACE_DIR": ("path", "the workspace root, checked against the production bind"),
    "SERAPH_PRODUCTION_MOUNT_SOURCE": ("server_process", "production container mount source check"),
    "SERAPH_PRODUCTION_BIND_IDENTITY": ("server_process", "production container bind identity check"),
    "FORWARDED_ALLOW_IPS": ("server_process", "auth startup validation of the ASGI server's proxy trust"),
    "SERVER_SOFTWARE": ("server_process", "auth startup validation of the ASGI server"),
    "UVICORN_HOST": ("server_process", "auth startup validation of the bind host"),
    "UVICORN_WORKERS": ("server_process", "auth startup validation of the worker count"),
    "WEB_CONCURRENCY": ("server_process", "auth startup validation of the worker count"),
    "GUNICORN_CMD_ARGS": ("server_process", "auth startup validation of the worker count"),
    "DBUS_SESSION_BUS_ADDRESS": ("platform", "passed through to the GitHub CLI's credential helper"),
    "XDG_CONFIG_HOME": ("platform", "the GitHub CLI's configuration directory"),
    "XDG_RUNTIME_DIR": ("platform", "the GitHub CLI's runtime directory"),
    "PLAYWRIGHT_BROWSERS_PATH": ("platform", "where the browser runtime is installed"),
    "SERAPH_SECURE_HOST_SHOULD_NOT_LEAK": ("test", "deterministic contract harness canary"),
})
# Reads whose variable name is computed at runtime, by ``module:function``.
DYNAMIC_ENV_READS: Mapping[str, str] = MappingProxyType({
    "src.llm_runtime:_redact_text": "provider key names from a literal tuple, read only to redact them",
    "src.llm_runtime:_secret_value": "a provider profile's credential env name (credentials only)",
    "src.tools.mcp_manager:MCPManager._resolve_env_vars": "${VAR} references in the operator's MCP config",
    f"{_ENV}:_env_enabled": "the ENV_READER_FUNCS helper; its callers' literal names are scanned",
    "src.model_fabric.contracts:ProviderProfile.api_key": "a profile's credential env name (credentials only)",
})
# Helpers whose argument at the given position is an environment variable name.
ENV_READER_FUNCS = frozenset({("_env_enabled", 0), ("_env_value", 1)})
_ENV_GET = frozenset({"os.environ.get", "os.getenv", "os.environ.setdefault", "os.environ.pop"})


def scan_env_flags(settings_file: Path = SETTINGS_FILE, launcher_file: Path = LAUNCHER_FILE) -> dict[str, str | None]:
    """{ENV_NAME: setting attribute or None}: boolean settings and the launcher's ``*_ENABLED`` flags."""
    tree = ast.parse(settings_file.read_text(), filename=str(settings_file))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    nested: dict[str, str] = {}  # attribute -> nested model class, e.g. repo_sandbox -> RepoSandboxSettings
    found: dict[str, str | None] = {}

    def fields(node: ast.ClassDef) -> list[tuple[str, ast.expr]]:
        return [(item.target.id, item.annotation) for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]

    settings_class = classes["Settings"]
    for name, annotation in fields(settings_class):
        if _is_bool(annotation):
            found[name.upper()] = name
        elif isinstance(annotation, ast.Name) and annotation.id in classes:
            nested[name] = annotation.id
    for prefix, class_name in nested.items():
        for name, annotation in fields(classes[class_name]):
            if _is_bool(annotation):
                found[f"{prefix}__{name}".upper()] = f"{prefix}__{name}"
    for name in _LAUNCHER_FLAG.findall(launcher_file.read_text()):
        found.setdefault(name, None)
    return found


def _is_bool(annotation: ast.expr | None) -> bool:
    """A boolean annotation, unwrapping Optional, Union, ``X | None``, Annotated and string forms."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _is_bool(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return False
    if isinstance(annotation, ast.Name):
        return annotation.id in {"bool", "StrictBool"}
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in {"bool", "StrictBool"}
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_bool(annotation.left) or _is_bool(annotation.right)
    if isinstance(annotation, ast.Subscript):
        base = dotted(annotation.value).rpartition(".")[2]
        members = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        if base in {"Optional", "Union"}:
            return any(_is_bool(member) for member in members)
        if base == "Annotated":
            return _is_bool(members[0])
    return False


def _module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def scan_env_reads(root: Path = SRC_ROOT) -> tuple[dict[str, set[str]], set[str]]:
    """({ENV_NAME: {module:function}}, {module:function with a computed name}) for ``backend/src``."""
    trees = {_module_name(path, root): ast.parse(path.read_text(), filename=str(path))
             for path in sorted(root.rglob("*.py"))}
    constants: dict[str, dict[str, ast.expr]] = {}
    imports: dict[str, dict[str, tuple[str, str]]] = {}
    for module, tree in trees.items():
        constants[module] = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                             for target in node.targets if isinstance(target, ast.Name)}
        imports[module] = {(alias.asname or alias.name): (node.module, alias.name) for node in tree.body
                           if isinstance(node, ast.ImportFrom) and node.module for alias in node.names}

    def resolve(module: str, node: ast.expr, depth: int = 0) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if not isinstance(node, ast.Name) or depth > 8:
            return None
        if node.id in constants.get(module, {}):
            return resolve(module, constants[module][node.id], depth + 1)
        source = imports.get(module, {}).get(node.id)
        if source and source[0] in constants and source[1] in constants[source[0]]:
            return resolve(source[0], constants[source[0]][source[1]], depth + 1)
        return None

    readers = dict(ENV_READER_FUNCS)
    found: dict[str, set[str]] = {}
    dynamic: set[str] = set()
    for module, tree in trees.items():
        # Best-effort aliases of os, os.environ and os.getenv in this module.
        os_names = {"os"} | {alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
                             for alias in node.names if alias.name == "os" and alias.asname}
        environ_names = {"os.environ", "environ"} | {f"{name}.environ" for name in os_names}
        getenv_names = {"getenv"} | {f"{name}.getenv" for name in os_names}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        environ_names.add(alias.asname or alias.name)
                    elif alias.name == "getenv":
                        getenv_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and dotted(node.value) in environ_names):
                environ_names.add(node.targets[0].id)
        environ_calls = {f"{name}.{method}" for name in environ_names for method in ("get", "setdefault", "pop")}

        def visit(node: ast.AST, scope: list[str]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = [*scope, node.name]
            name_node = None
            if isinstance(node, ast.Call) and node.args:
                func = dotted(node.func)
                if func in _ENV_GET or func in environ_calls or func in getenv_names:
                    name_node = node.args[0]
                elif func in readers and len(node.args) > readers[func]:
                    name_node = node.args[readers[func]]
            elif isinstance(node, ast.Subscript) and dotted(node.value) in environ_names:
                name_node = node.slice
            elif (isinstance(node, ast.Compare) and len(node.comparators) == 1
                  and isinstance(node.ops[0], (ast.In, ast.NotIn))
                  and dotted(node.comparators[0]) in environ_names):
                name_node = node.left
            if name_node is not None:
                where = f"{module}:{'.'.join(scope) or '<module>'}"
                name = resolve(module, name_node)
                if name is None:
                    dynamic.add(where)
                else:
                    found.setdefault(name, set()).add(where)
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        visit(tree, [])
    return found, dynamic


def env_flag_problems(flags: tuple[EnvFlag, ...] = ENV_FLAGS) -> list[str]:
    """Contradictions between the flag table, the patch-row catalogue and the security set."""
    from .ownership import DOMAINS
    from .profiles import PATCH_ROWS, SECURITY_SETTINGS
    problems: list[str] = []
    keys = set(rows())
    seen: set[str] = set()
    for flag in flags:
        label = flag.env_name
        if label in seen:
            problems.append(f"{label}: listed twice")
        seen.add(label)
        if flag.kind not in ENV_FLAG_KINDS:
            problems.append(f"{label}: kind_unknown")
        if not flag.reason.strip() or "\n" in flag.reason:
            problems.append(f"{label}: reason_not_one_line")
        problems += [f"{label}: gate {gate} is not an inventory row" for gate in flag.gates if gate not in keys]
        spec = PATCH_ROWS.get(flag.row_id) if flag.row_id else None
        if flag.kind in {"row", "removed"}:
            if spec is None:
                problems.append(f"{label}: row_missing")
                continue
            if (spec.env_name, spec.env_setting) != (flag.env_name, flag.setting):
                problems.append(f"{label}: row_env_mismatch")
            if spec.runtime_domain not in DOMAINS:
                problems.append(f"{label}: row_domain_unknown")
            if not _ISSUE_REF.fullmatch(spec.removal_issue):
                problems.append(f"{label}: removal_issue_missing")
            if (spec.env_stage == "retired") is not (flag.kind == "removed"):
                problems.append(f"{label}: removed_stage_mismatch")
        elif flag.row_id is not None:
            problems.append(f"{label}: row_on_non_row_flag")
        if flag.kind == "security_switch" and label.lower() not in SECURITY_SETTINGS:
            problems.append(f"{label}: not_in_security_settings")
    row_flags = {flag.row_id for flag in flags if flag.kind in {"row", "removed"}}
    problems += [f"{row_id}: patch row has no flag entry" for row_id in PATCH_ROWS if row_id not in row_flags]
    return problems


_ISSUE_REF = re.compile(r"#[1-9][0-9]*")


def row_problems(row: Owner | Gap | Exclusion) -> list[str]:
    """Why one row is invalid; empty when it is consistent with the domain, bundle and owner catalogues."""
    from .ownership import DOMAINS
    from .profiles import BUNDLE_NAMES
    from .trust import DOMAIN_BUNDLES, TRUST_BUNDLE
    problems: list[str] = []
    module, _, symbol = row.key.partition(":")
    if not module.startswith("src") or not symbol:
        problems.append("key_invalid")
    if not row.shapes or not row.shapes <= SHAPES:
        problems.append("shape_unknown")
    if isinstance(row, Owner):
        if row.domain not in DOMAINS:
            problems.append("owner_domain_unknown")
        elif row.bundle != DOMAIN_BUNDLES.get(row.domain):
            problems.append("owner_bundle_mismatch")
        if row.bundle not in BUNDLE_NAMES:
            problems.append("bundle_unknown")
        if row.required is not (row.bundle == TRUST_BUNDLE):
            # Required exactly for the pinned seraph-trust services (ADR-036 §3).
            problems.append("owner_required_mismatch")
    if isinstance(row, (Owner, Gap)) and row.provider not in PROVIDERS:
        problems.append("provider_unknown")
    if isinstance(row, Gap):
        if row.target in DOMAINS:
            problems.append("gap_has_owner_domain")
        if row.target not in PLANNED_SEAMS:
            problems.append("gap_target_unknown")
        elif row.bundle != PLANNED_SEAMS[row.target]:
            problems.append("gap_bundle_mismatch")
        if row.bundle is not None and row.bundle not in BUNDLE_NAMES:
            problems.append("bundle_unknown")
        if row.owner not in GAP_OWNERS:
            problems.append("gap_owner_unknown")
        if not _ISSUE_REF.fullmatch(row.owner):
            problems.append("gap_owner_untracked")
    if isinstance(row, (Gap, Exclusion)) and (not row.reason.strip() or "\n" in row.reason):
        problems.append("reason_not_one_line")
    if isinstance(row, Exclusion) and row.kind not in EXCLUSION_KINDS:
        problems.append("exclusion_kind_unknown")
    return problems


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
#
# Names are resolved through the module's own imports (``import subprocess as
# sp``, ``from subprocess import run``, ``from concurrent import futures``), so
# an alias reaches the same canonical name. Known limitation: a call reached
# only dynamically is not seen, i.e. through ``getattr``, ``importlib``,
# ``__import__``, ``exec``/``eval``, or a callable first stored in a variable
# or attribute (``spawn = subprocess.Popen; spawn(...)``). Review catches those.

LIFESPAN_MODULE = "src.app"
LIFESPAN_FUNCTIONS = frozenset({"lifespan", "_provider_worker_lifespan", "_lifespan_services"})
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route"})
# Canonical subprocess forms. ``os.fork``/``forkpty``/``spawn*``/``exec*``/``posix_spawn*`` match by prefix.
SUBPROCESS_CALLS = frozenset({
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "os.system", "os.popen", "pty.spawn", "pty.fork",
    "multiprocessing.Process", "multiprocessing.Pool", "concurrent.futures.ProcessPoolExecutor",
})
_OS_SPAWN = ("os.fork", "os.forkpty", "os.spawn", "os.exec", "os.posix_spawn")
# Loop methods that spawn a child: ``loop.subprocess_exec``/``subprocess_shell``.
_LOOP_SUBPROCESS = frozenset({"subprocess_exec", "subprocess_shell"})
# Canonical thread forms, plus any ``.run_in_executor(...)``.
THREAD_CALLS = frozenset({"threading.Thread", "threading.Timer", "concurrent.futures.ThreadPoolExecutor",
                          "asyncio.to_thread"})
# Process-wide hooks: exit handlers and signal handlers, plus any ``.add_signal_handler(...)``.
PROCESS_HOOK_CALLS = frozenset({"atexit.register", "signal.signal"})
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


def _imports(tree: ast.AST) -> dict[str, str]:
    """Local name -> canonical dotted name, from every import in the module."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str, aliases: dict[str, str]):
        self.module = module
        self.aliases = aliases
        self.scope: list[str] = []
        self.task_groups: set[str] = set()
        self.found: set[tuple[str, str]] = set()

    def canonical(self, node: ast.AST) -> str:
        name = dotted(node)
        head, dot, rest = name.partition(".")
        target = self.aliases.get(head)
        return f"{target}{dot}{rest}" if target else name

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
                own = ".".join([*self.scope, node.name])
                if attr in {"websocket", "websocket_route"}:
                    self._add(own, "websocket")
                elif attr == "on_event":
                    self._add(own, "lifespan_step")
                elif attr in _HTTP_METHODS and isinstance(decorator.func.value, ast.Name) \
                        and decorator.func.value.id == "app":
                    self._add(own, "router")
        self.scope.append(node.name)
        if self.module == LIFESPAN_MODULE and len(self.scope) == 1 and node.name in LIFESPAN_FUNCTIONS:
            self._lifespan_steps(node)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call) and isinstance(item.optional_vars, ast.Name) \
                    and self.canonical(item.context_expr.func) == "asyncio.TaskGroup":
                self.task_groups.add(item.optional_vars.id)
        self.generic_visit(node)

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

    def _is_task_spawn(self, func: ast.AST, canonical: str) -> bool:
        name = _callee_name(func)
        if name == "track_task" or canonical in {"asyncio.create_task", "asyncio.ensure_future"}:
            return True
        if name not in {"create_task", "ensure_future"}:
            return False
        if isinstance(func, ast.Name):
            return True
        # ``loop.create_task`` and a TaskGroup's ``tg.create_task``, never ``repository.create_task``.
        base = dotted(func.value) if isinstance(func, ast.Attribute) else ""
        return (base == "asyncio" or base.endswith(("loop", "loop()", "_loop")) or base.startswith("asyncio.get_")
                or base in self.task_groups or self.canonical(func.value).endswith("asyncio.TaskGroup()"))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _callee_name(func)
        canonical = self.canonical(func)
        if name == "add_job":
            self._add(self._symbol(), "scheduler_job")
        elif name == "APIRouter":
            self._add(self._symbol(), "router")
        elif name == "add_api_route":
            self._add(self._symbol(), "router")
        elif name in {"add_api_websocket_route", "add_websocket_route"}:
            self._add(self._symbol(), "websocket")
        elif name == "add_event_handler":
            self._add(self._symbol(), "lifespan_step")
        elif name == "FastAPI":
            self._add(self._symbol(), "app_factory")
        elif self._is_task_spawn(func, canonical):
            self._add(self._symbol(), "background_task")
        elif canonical in THREAD_CALLS or name == "run_in_executor" or canonical == "Thread":
            self._add(self._symbol(), "thread")
        elif (canonical in SUBPROCESS_CALLS or canonical.startswith(_OS_SPAWN) or name in _LOOP_SUBPROCESS
              or (canonical.startswith("multiprocessing.") and name in {"Process", "Pool"})
              or canonical in {"Popen", "create_subprocess_exec", "create_subprocess_shell"}):
            self._add(self._symbol(), "subprocess")
        elif canonical in PROCESS_HOOK_CALLS or name == "add_signal_handler":
            self._add(self._symbol(), "process_hook")
        self.generic_visit(node)


def scan_lifecycle_shapes(root: Path | None = None) -> set[tuple[str, str]]:
    """Every ``(module:symbol, shape)`` lifecycle finding under ``root`` (default ``backend/src``)."""
    source_root = SRC_ROOT if root is None else Path(root)
    found: set[tuple[str, str]] = set()
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = _Scanner(module_name(path, source_root), _imports(tree))
        scanner.visit(tree)
        found |= scanner.found
    return found


def unaccounted(found: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Scan findings in no table: each one is a runtime owner outside the inventory."""
    return sorted(found - accounted())


def stale(found: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Table rows the scan no longer finds: delete or rename them."""
    return sorted(accounted() - found)


__all__ = ["EXCLUSIONS", "EXCLUSION_KINDS", "Exclusion", "GAPS", "GAP_OWNERS", "Gap", "OWNERS", "Owner",
           "PLANNED_SEAMS", "PROCESS_HOOK_CALLS", "PROVIDERS", "SHAPES", "SUBPROCESS_CALLS", "THREAD_CALLS",
           "accounted", "row_problems", "rows", "scan_lifecycle_shapes", "stale",
           "unaccounted"]
