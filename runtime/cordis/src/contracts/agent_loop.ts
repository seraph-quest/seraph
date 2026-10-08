import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.agent-loop.v1' as const;
export interface SeraphAgentLoop {
  startTurn(input: Input<'agent-loop.startTurn'>): Promise<Result<'agent-loop.startTurn'>>;
  cancelTurn(input: Input<'agent-loop.cancelTurn'>): Promise<Result<'agent-loop.cancelTurn'>>;
  inspectTurn(input: Input<'agent-loop.inspectTurn'>): Promise<Result<'agent-loop.inspectTurn'>>;
}
declare module 'cordis' { interface Context { seraphAgentLoop: SeraphAgentLoop; } }
