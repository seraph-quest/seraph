import type { Context } from "cordis";
import type { ScopedRequestClient } from "../contracts/client.js";
import type { Input, Result, ServiceMethod } from "../contracts/methods.js";
import { createPlugin as authority } from "./authority/index.js";
import { createPlugin as goals } from "./goals/index.js";
import { createPlugin as tasks } from "./tasks/index.js";
import { createPlugin as capabilities } from "./capabilities/index.js";
import { createPlugin as inference } from "./inference/index.js";
import { createPlugin as memory } from "./memory/index.js";
import { createPlugin as artifacts } from "./artifacts/index.js";
import { createPlugin as audit } from "./audit/index.js";
import { createPlugin as research } from "./research/index.js";
import { createPlugin as conversation } from "./conversation/index.js";
import { createPlugin as scheduler } from "./scheduler/index.js";
import { createPlugin as connections } from "./connections/index.js";
import { createPlugin as agent_loop } from "./agent_loop/index.js";
import { createPlugin as source_extraction } from "./source_extraction/index.js";
export const SERVICE_KEYS = [
  "seraphAuthority",
  "seraphGoals",
  "seraphTasks",
  "seraphCapabilities",
  "seraphInference",
  "seraphMemory",
  "seraphArtifacts",
  "seraphAudit",
  "seraphResearch",
  "seraphConversation",
  "seraphScheduler",
  "seraphConnections",
  "seraphAgentLoop",
  "seraphSourceExtraction",
] as const;
export function servicePlugins(client: ScopedRequestClient) { return {
  "seraph.authority.v1": authority(client),
  "seraph.goals.v1": goals(client),
  "seraph.tasks.v1": tasks(client),
  "seraph.capabilities.v1": capabilities(client),
  "seraph.inference.v1": inference(client),
  "seraph.memory.v1": memory(client),
  "seraph.artifacts.v1": artifacts(client),
  "seraph.audit.v1": audit(client),
  "seraph.research.v1": research(client),
  "seraph.conversation.v1": conversation(client),
  "seraph.scheduler.v1": scheduler(client),
  "seraph.connections.v1": connections(client),
  "seraph.agent-loop.v1": agent_loop(client),
  "seraph.source-extraction.v1": source_extraction(client),
} as const; }
const invokers: {[M in ServiceMethod]: (ctx: Context, input: Input<ServiceMethod>) => Promise<Result<ServiceMethod>>} = {
  "authority.resolve": (ctx, input) => ctx.seraphAuthority.resolve(input as Input<"authority.resolve">),
  "goals.read": (ctx, input) => ctx.seraphGoals.read(input as Input<"goals.read">),
  "tasks.admit": (ctx, input) => ctx.seraphTasks.admit(input as Input<"tasks.admit">),
  "tasks.inspect": (ctx, input) => ctx.seraphTasks.inspect(input as Input<"tasks.inspect">),
  "tasks.cancel": (ctx, input) => ctx.seraphTasks.cancel(input as Input<"tasks.cancel">),
  "tasks.checkpoint": (ctx, input) => ctx.seraphTasks.checkpoint(input as Input<"tasks.checkpoint">),
  "tasks.settle": (ctx, input) => ctx.seraphTasks.settle(input as Input<"tasks.settle">),
  "capabilities.list": (ctx, input) => ctx.seraphCapabilities.list(input as Input<"capabilities.list">),
  "capabilities.describe": (ctx, input) => ctx.seraphCapabilities.describe(input as Input<"capabilities.describe">),
  "capabilities.invoke": (ctx, input) => ctx.seraphCapabilities.invoke(input as Input<"capabilities.invoke">),
  "inference.request": (ctx, input) => ctx.seraphInference.request(input as Input<"inference.request">),
  "memory.retrieve": (ctx, input) => ctx.seraphMemory.retrieve(input as Input<"memory.retrieve">),
  "memory.propose": (ctx, input) => ctx.seraphMemory.propose(input as Input<"memory.propose">),
  "memory.applyReviewed": (ctx, input) => ctx.seraphMemory.applyReviewed(input as Input<"memory.applyReviewed">),
  "memory.forget": (ctx, input) => ctx.seraphMemory.forget(input as Input<"memory.forget">),
  "artifacts.read": (ctx, input) => ctx.seraphArtifacts.read(input as Input<"artifacts.read">),
  "artifacts.stage": (ctx, input) => ctx.seraphArtifacts.stage(input as Input<"artifacts.stage">),
  "artifacts.adopt": (ctx, input) => ctx.seraphArtifacts.adopt(input as Input<"artifacts.adopt">),
  "audit.append": (ctx, input) => ctx.seraphAudit.append(input as Input<"audit.append">),
  "research.buildPlan": (ctx, input) => ctx.seraphResearch.buildPlan(input as Input<"research.buildPlan">),
  "research.executeAccepted": (ctx, input) => ctx.seraphResearch.executeAccepted(input as Input<"research.executeAccepted">),
  "conversation.accept": (ctx, input) => ctx.seraphConversation.accept(input as Input<"conversation.accept">),
  "conversation.append": (ctx, input) => ctx.seraphConversation.append(input as Input<"conversation.append">),
  "conversation.read": (ctx, input) => ctx.seraphConversation.read(input as Input<"conversation.read">),
  "conversation.cancel": (ctx, input) => ctx.seraphConversation.cancel(input as Input<"conversation.cancel">),
  "scheduler.register": (ctx, input) => ctx.seraphScheduler.register(input as Input<"scheduler.register">),
  "scheduler.disable": (ctx, input) => ctx.seraphScheduler.disable(input as Input<"scheduler.disable">),
  "scheduler.dispatchDue": (ctx, input) => ctx.seraphScheduler.dispatchDue(input as Input<"scheduler.dispatchDue">),
  "connections.inspect": (ctx, input) => ctx.seraphConnections.inspect(input as Input<"connections.inspect">),
  "connections.invokeBoundAdapter": (ctx, input) => ctx.seraphConnections.invokeBoundAdapter(input as Input<"connections.invokeBoundAdapter">),
  "agent-loop.startTurn": (ctx, input) => ctx.seraphAgentLoop.startTurn(input as Input<"agent-loop.startTurn">),
  "agent-loop.cancelTurn": (ctx, input) => ctx.seraphAgentLoop.cancelTurn(input as Input<"agent-loop.cancelTurn">),
  "agent-loop.inspectTurn": (ctx, input) => ctx.seraphAgentLoop.inspectTurn(input as Input<"agent-loop.inspectTurn">),
  "source-extraction.extract": (ctx, input) => ctx.seraphSourceExtraction.extract(input as Input<"source-extraction.extract">),
};
export function invokeService<M extends ServiceMethod>(ctx: Context, method: M, input: Input<M>): Promise<Result<M>> { return invokers[method](ctx, input) as Promise<Result<M>>; }
