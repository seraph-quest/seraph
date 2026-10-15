import { Context, Service, FiberState, type Fiber } from 'cordis';
import { ProtocolError } from '../protocol.js';
import { validateInput, validateResult, type Input, type Result, type ServiceMethod } from '../contracts/methods.js';
import type { ScopedRequestClient } from '../contracts/client.js';

/** Forwarding only: native authority, branches and effects remain Python-owned. */
export abstract class NativeProxy extends Service {
  private readonly registrationFiber: Fiber;
  constructor(ctx: Context, private readonly key: string, private readonly client: ScopedRequestClient) { super(ctx, key); this.registrationFiber = ctx.fiber; }
  protected async forward<M extends ServiceMethod>(method: M, input: Input<M>): Promise<Result<M>> {
    const assertActive = () => { if (this.registrationFiber.state !== FiberState.ACTIVE || !this.ctx.get(this.key) || !this.client.isActive()) throw new ProtocolError('service invocation unavailable'); };
    assertActive();
    const payload = validateInput(method, input);
    const response = await this.client.request(method, payload);
    assertActive();
    return validateResult(method, response);
  }
}
