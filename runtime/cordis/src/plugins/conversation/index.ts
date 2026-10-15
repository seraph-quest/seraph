import { Context, type Plugin } from 'cordis';
import type { SeraphConversation } from '../../contracts/conversation.js';
import type { Input, Result } from '../../contracts/methods.js';
import type { ScopedRequestClient } from '../../contracts/client.js';
import { NativeProxy } from '../proxy.js';

export class SeraphConversationService extends NativeProxy implements SeraphConversation {
  constructor(ctx: Context, client: ScopedRequestClient) { super(ctx, 'seraphConversation', client); }
  accept(input: Input<'conversation.accept'>): Promise<Result<'conversation.accept'>> { return this.forward('conversation.accept', input); }
  append(input: Input<'conversation.append'>): Promise<Result<'conversation.append'>> { return this.forward('conversation.append', input); }
  read(input: Input<'conversation.read'>): Promise<Result<'conversation.read'>> { return this.forward('conversation.read', input); }
  cancel(input: Input<'conversation.cancel'>): Promise<Result<'conversation.cancel'>> { return this.forward('conversation.cancel', input); }
}
/** Trusted composition injects the original invocation-scoped transport. */
export function createPlugin(client: ScopedRequestClient): Plugin.Object<Record<string, never>> {
  return { name: 'seraph.conversation.v1', provide: ['seraphConversation'], apply(ctx) { new SeraphConversationService(ctx, client); } };
}
