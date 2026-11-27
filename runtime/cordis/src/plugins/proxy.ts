import { Context, Service, FiberState, type Fiber } from 'cordis';
import { ProtocolError } from '../protocol.js';
import { FACADE_DOMAINS, validateInput, validateResult, type FacadeDomain, type Input, type Result, type ServiceMethod } from '../contracts/methods.js';
import type { ScopedRequestClient } from '../contracts/client.js';

/** This boot's facade activations (#1024). A facade the host never asked
 * about keeps forwarding (its domain is legacy-owned and Python decides); one
 * the host deactivated, or whose activation this child refused, forwards
 * nothing until the host activates it again at an epoch. */
export class FacadeActivations {
  private readonly entries = new Map<FacadeDomain, { state: 'activated' | 'inactive'; epoch: number }>();
  activate(domain: FacadeDomain, epoch: number): void { this.entries.set(domain, Object.freeze({ state: 'activated', epoch })); }
  deactivate(domain: FacadeDomain, epoch: number): void { this.entries.set(domain, Object.freeze({ state: 'inactive', epoch })); }
  get(domain: FacadeDomain): { state: 'activated' | 'inactive'; epoch: number } | undefined { return this.entries.get(domain); }
  inactive(domain: string): boolean { return (FACADE_DOMAINS as readonly string[]).includes(domain) && this.entries.get(domain as FacadeDomain)?.state === 'inactive'; }
  clear(): void { this.entries.clear(); }
}
export const facadeActivations = new FacadeActivations();

/** Forwarding only: native authority, branches and effects remain Python-owned. */
export abstract class NativeProxy extends Service {
  private readonly registrationFiber: Fiber;
  constructor(ctx: Context, private readonly key: string, private readonly client: ScopedRequestClient) { super(ctx, key); this.registrationFiber = ctx.fiber; }
  protected async forward<M extends ServiceMethod>(method: M, input: Input<M>): Promise<Result<M>> {
    const assertActive = () => { if (this.registrationFiber.state !== FiberState.ACTIVE || !this.ctx.get(this.key) || !this.client.isActive()) throw new ProtocolError('service invocation unavailable'); };
    assertActive();
    // A deactivated facade forwards nothing; Python rechecks the activation anyway.
    if (facadeActivations.inactive(`seraph.${method.slice(0, method.indexOf('.'))}.v1`)) return { status: 'blocked', reason_code: 'provider_facade_inactive', memory_status: 'no_learning' } as Result<M>;
    const payload = validateInput(method, input);
    const response = await this.client.request(method, payload);
    assertActive();
    return validateResult(method, response);
  }
}
