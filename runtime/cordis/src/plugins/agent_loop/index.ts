import { Context, type Plugin } from 'cordis';
import type { SeraphAgentLoop } from '../../contracts/agent_loop.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphAgentLoopService extends NativeProxy implements SeraphAgentLoop {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphAgentLoop', client); }
  startTurn(input: Input<'agent-loop.startTurn'>): Promise<Result<'agent-loop.startTurn'>> { return this.forward('agent-loop.startTurn', input); }
  cancelTurn(input: Input<'agent-loop.cancelTurn'>): Promise<Result<'agent-loop.cancelTurn'>> { return this.forward('agent-loop.cancelTurn', input); }
  inspectTurn(input: Input<'agent-loop.inspectTurn'>): Promise<Result<'agent-loop.inspectTurn'>> { return this.forward('agent-loop.inspectTurn', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.agent-loop.v1', provide: ['seraphAgentLoop'], apply(ctx) { new SeraphAgentLoopService(ctx, client); } };
}
