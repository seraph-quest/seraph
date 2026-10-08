import type { Input, Result, ServiceMethod } from './methods.js';

/** Trusted owner creates this per native invocation. No caller frame fields. */
export interface ScopedRequestClient {
  isActive(): boolean;
  request<M extends ServiceMethod>(method: M, input: Input<M>): Promise<unknown>;
}
export interface ServiceCaller {
  call<M extends ServiceMethod>(method: M, input: Input<M>): Promise<Result<M>>;
}
