import type { Input, Result } from './methods.js';

export const DOMAIN = 'seraph.conversation.v1' as const;
export interface SeraphConversation {
  accept(input: Input<'conversation.accept'>): Promise<Result<'conversation.accept'>>;
  append(input: Input<'conversation.append'>): Promise<Result<'conversation.append'>>;
  read(input: Input<'conversation.read'>): Promise<Result<'conversation.read'>>;
  cancel(input: Input<'conversation.cancel'>): Promise<Result<'conversation.cancel'>>;
}
declare module 'cordis' { interface Context { seraphConversation: SeraphConversation; } }
