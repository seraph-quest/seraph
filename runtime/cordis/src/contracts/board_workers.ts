import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.board-workers.v1' as const;
export interface SeraphBoardWorkers {
  startTurn(input: Input<'board-workers.startTurn'>): Promise<Result<'board-workers.startTurn'>>;
}
declare module 'cordis' { interface Context { seraphBoardWorkers: SeraphBoardWorkers; } }
