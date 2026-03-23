from __future__ import annotations

from smolagents import ToolCallingAgent

from config.settings import settings
from src.guardian.state import GuardianState
from src.llm_runtime import FallbackLiteLLMModel as LiteLLMModel, build_model_kwargs
from src.native_tools.loader import discover_tools
from src.skills.manager import skill_manager
from src.tools.approval import wrap_tools_for_approval, wrap_tools_with_forced_approval
from src.tools.audit import wrap_tools_for_audit
from src.tools.policy import filter_tools, get_current_mcp_policy_mode, get_current_tool_policy_mode
from src.tools.mcp_manager import mcp_manager
from src.tools.secret_ref_tools import wrap_tools_for_secret_refs
from src.workflows.manager import workflow_manager


def get_model(*, runtime_path: str = "chat_agent") -> LiteLLMModel:
    """Create a LiteLLMModel from the shared runtime configuration."""
    return LiteLLMModel(**build_model_kwargs(
        temperature=settings.model_temperature,
        max_tokens=settings.model_max_tokens,
        runtime_path=runtime_path,
    ))


def get_base_tools_and_active_skills() -> tuple[list, list[str], str]:
    """Return the executable non-workflow tools plus the active skill names."""
    tool_mode = get_current_tool_policy_mode()
    mcp_mode = get_current_mcp_policy_mode()
    native_tools = wrap_tools_for_approval(
        wrap_tools_for_audit(
            wrap_tools_for_secret_refs(filter_tools(discover_tools(), tool_mode))
        )
    )
    filtered_mcp_tools = filter_tools(
        mcp_manager.get_tools(),
        tool_mode,
        is_mcp=True,
        mcp_mode=mcp_mode,
    )
    if mcp_mode == "approval":
        mcp_tools = wrap_tools_with_forced_approval(
            wrap_tools_for_audit(
                wrap_tools_for_secret_refs(filtered_mcp_tools),
                treat_all_as_mcp=True,
            ),
            treat_all_as_mcp=True,
        )
    else:
        mcp_tools = wrap_tools_for_approval(
            wrap_tools_for_audit(
                wrap_tools_for_secret_refs(filtered_mcp_tools),
                treat_all_as_mcp=True,
            ),
            treat_all_as_mcp=True,
        )
    base_tools = native_tools + mcp_tools
    active_skill_names = [
        skill.name
        for skill in skill_manager.get_active_skills([tool.name for tool in base_tools])
    ]
    return base_tools, active_skill_names, mcp_mode


def _append_workflow_tools(base_tools: list, active_skill_names: list[str], mcp_mode: str) -> list:
    """Build workflow tools against the executable base tool surface."""
    workflow_tools = wrap_tools_for_audit(
        workflow_manager.build_workflow_tools(base_tools, active_skill_names)
    )
    forced_approval_workflows: list = []
    normal_workflows: list = []
    workflow_risk_overrides: dict[str, str] = {}
    for tool in workflow_tools:
        metadata = workflow_manager.get_tool_metadata(tool.name) or {}
        boundaries = metadata.get("execution_boundaries", [])
        risk_level = metadata.get("risk_level")
        if isinstance(risk_level, str):
            workflow_risk_overrides[tool.name] = risk_level
        if mcp_mode == "approval" and "external_mcp" in boundaries:
            forced_approval_workflows.append(tool)
        else:
            normal_workflows.append(tool)
    workflow_tools = wrap_tools_for_approval(
        normal_workflows,
        risk_overrides=workflow_risk_overrides,
    )
    if forced_approval_workflows:
        workflow_tools += wrap_tools_with_forced_approval(
            forced_approval_workflows,
            risk_overrides=workflow_risk_overrides,
        )
    return base_tools + workflow_tools


def get_tools() -> list:
    """Return all auto-discovered tools + MCP tools."""
    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    return _append_workflow_tools(base_tools, active_skill_names, mcp_mode)


def create_agent(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
    guardian_state: GuardianState | None = None,
) -> ToolCallingAgent:
    """Create a ToolCallingAgent with LiteLLM model and tools.

    Args:
        additional_context: Conversation history to include in the system prompt.
        soul_context: Guardian record content (user identity, values, goals).
        memory_context: Relevant long-term memories for this conversation.
        observer_context: Current observer context (time, window, screen, etc.).
    """
    model = get_model(runtime_path="chat_agent")
    tools = get_tools()
    tool_names = [t.name for t in tools]

    instructions = (
        "You are Seraph, a proactive guardian intelligence operating a dense human workspace. "
        "Observe carefully, think ahead, and act to help your human counterpart maintain "
        "clarity, follow-through, and sound judgment across productivity, performance, health, "
        "influence, and growth. Treat relationship or collaboration priorities as influence unless "
        "another supported domain fits better. Be concise, exact, strategic, and useful."
    )
    if guardian_state is not None:
        soul_context = guardian_state.soul_context
        memory_context = guardian_state.memory_context
        additional_context = guardian_state.current_session_history or additional_context
        instructions += f"\n\n--- GUARDIAN STATE ---\n{guardian_state.to_prompt_block()}"
    elif observer_context:
        instructions += f"\n\n--- CURRENT CONTEXT ---\n{observer_context}"

    if soul_context:
        instructions += f"\n\n--- USER IDENTITY ---\n{soul_context}"
    if memory_context:
        instructions += f"\n\n--- RELEVANT MEMORIES ---\n{memory_context}"

    # Inject active skills into instructions
    active_skills = skill_manager.get_active_skills(tool_names)
    if active_skills:
        skill_lines = []
        for s in active_skills:
            invocable = " [user-invocable]" if s.user_invocable else ""
            skill_lines.append(f"### Skill: {s.name}{invocable}\n{s.instructions}")
        instructions += "\n\n## Available Skills\n\n" + "\n\n".join(skill_lines)

    if additional_context:
        instructions += f"\n\n--- CONVERSATION HISTORY ---\n{additional_context}"

    agent = ToolCallingAgent(
        tools=tools,
        model=model,
        max_steps=settings.agent_max_steps,
        instructions=instructions,
    )
    return agent


def create_orchestrator(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
    guardian_state: GuardianState | None = None,
) -> ToolCallingAgent:
    """Create an orchestrator agent that delegates to specialist sub-agents.

    The orchestrator has NO tools itself — it delegates all execution to
    specialist managed_agents (memory_keeper, goal_planner, web_researcher,
    file_worker, and one per MCP server).
    """
    from src.agent.specialists import build_all_specialists

    model = get_model(runtime_path="orchestrator_agent")
    specialists = build_all_specialists()

    # Collect all tool names across specialists for skill gating
    all_tool_names = []
    for specialist in specialists:
        all_tool_names.extend(t.name for t in specialist.tools)

    instructions = (
        "You are Seraph, a proactive guardian intelligence operating a dense human workspace. "
        "Observe carefully, think ahead, and act to help your human counterpart maintain "
        "clarity, follow-through, and sound judgment across productivity, performance, health, "
        "influence, and growth. Treat relationship or collaboration priorities as influence unless "
        "another supported domain fits better. Be concise, exact, strategic, and useful.\n\n"
        "You do NOT have any tools yourself. Instead, you have a team of specialists.\n"
        "Analyze the user's request, decide which specialist(s) to delegate to, and\n"
        "synthesize their results into a coherent, helpful response.\n"
        "Guidelines:\n"
        "- For simple questions that need no tools, answer directly.\n"
        "- Delegate to ONE specialist when possible.\n"
        "- Give clear, specific task descriptions when delegating.\n"
        "- Synthesize specialist results into a natural response."
    )
    if guardian_state is not None:
        soul_context = guardian_state.soul_context
        memory_context = guardian_state.memory_context
        additional_context = guardian_state.current_session_history or additional_context
        instructions += f"\n\n--- GUARDIAN STATE ---\n{guardian_state.to_prompt_block()}"
    elif observer_context:
        instructions += f"\n\n--- CURRENT CONTEXT ---\n{observer_context}"

    if soul_context:
        instructions += f"\n\n--- USER IDENTITY ---\n{soul_context}"
    if memory_context:
        instructions += f"\n\n--- RELEVANT MEMORIES ---\n{memory_context}"

    # Inject active skills into instructions
    active_skills = skill_manager.get_active_skills(all_tool_names)
    if active_skills:
        skill_lines = []
        for s in active_skills:
            invocable = " [user-invocable]" if s.user_invocable else ""
            skill_lines.append(f"### Skill: {s.name}{invocable}\n{s.instructions}")
        instructions += "\n\n## Available Skills\n\n" + "\n\n".join(skill_lines)

    if additional_context:
        instructions += f"\n\n--- CONVERSATION HISTORY ---\n{additional_context}"

    agent = ToolCallingAgent(
        tools=[],
        managed_agents=specialists,
        model=model,
        max_steps=settings.orchestrator_max_steps,
        instructions=instructions,
    )
    return agent


def build_agent(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
    guardian_state: GuardianState | None = None,
) -> ToolCallingAgent:
    """Build the appropriate agent based on delegation feature flag.

    Drop-in replacement for create_agent() at call sites.
    """
    if settings.use_delegation:
        if guardian_state is not None:
            return create_orchestrator(
                additional_context,
                soul_context,
                memory_context,
                observer_context,
                guardian_state=guardian_state,
            )
        return create_orchestrator(
            additional_context,
            soul_context,
            memory_context,
            observer_context,
        )
    if guardian_state is not None:
        return create_agent(
            additional_context,
            soul_context,
            memory_context,
            observer_context,
            guardian_state=guardian_state,
        )
    return create_agent(
        additional_context,
        soul_context,
        memory_context,
        observer_context,
    )
