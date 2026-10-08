/** Trusted finite two-way bootstrap; native state and execution remain Python-owned. */
import { Composition, packageIdentity } from './composition.js';
import { CONTROL_TIMEOUT_MS, MAX_PENDING, ProtocolError, readFrames, writeFrame, type Frame, type Json, type Method } from './protocol.js';
import { isServiceMethod, validateInput, validateResult, type Input, type ServiceMethod } from './contracts/methods.js';
import type { ScopedRequestClient } from './contracts/client.js';
import { Resources } from './resources.js';

interface Pending { frame: Frame; resolve(value: unknown): void; reject(error: Error): void; }
async function main(): Promise<void> {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (!((major === 22 && (minor ?? 0) >= 12) || major === 24)) throw new ProtocolError("unsupported Node");
  const identity = packageIdentity();
  const composition = new Composition(identity.profile);
  const pipes = new Resources();
  pipes.track("owned-input", () => { process.stdin.destroy(); });
  const pending = new Map<string, Pending>();
  const scopes = new Map<string, { frame: Frame; active: boolean; done: Promise<void> }>();
  let nonce: string | undefined, inboundSeq = 0, outboundSeq = 0;
  let quiescing = false, shutDown = false, failure: Error | undefined;
  let writeQueue = Promise.resolve();
  const send = (frame: Frame): Promise<void> => {
    // Sequence assignment and pipe writes use one ordered queue in both directions.
    writeQueue = writeQueue.then(() => writeFrame(process.stdout, frame, pipes));
    return writeQueue;
  };
  const response = (request: Frame, method: Method, payload: Record<string, Json>) => send({ ...request, seq: ++outboundSeq, kind: "response", method, payload });
  const fence = (error: Error) => {
    failure ??= error; quiescing = true;
    for (const scope of scopes.values()) scope.active = false;
    for (const [id, item] of pending) { pending.delete(id); item.reject(error); void pipes.release(`service-${id}`).catch(() => {}); }
    process.stdin.destroy();
  };
  const invoke = (incoming: Frame & { method: ServiceMethod }) => {
    if (pending.size + scopes.size >= MAX_PENDING || quiescing) return response(incoming, incoming.method, {status: "blocked", reason_code: quiescing ? "runtime_quiescing" : "bridge_capacity_exceeded", memory_status: "no_learning"});
    const scope = { frame: incoming, active: true, done: Promise.resolve() };
    const client: ScopedRequestClient = {
      isActive: () => scope.active && !quiescing && !failure && incoming.deadline_at > Date.now(),
      request: async <M extends ServiceMethod>(method: M, input: Input<M>) => {
        if (!client.isActive() || method !== incoming.method) throw new ProtocolError("stale native service scope");
        if (pending.size + scopes.size >= MAX_PENDING) return {status: "blocked", reason_code: "bridge_capacity_exceeded", memory_status: "no_learning"};
        const seq = ++outboundSeq;
        const request: Frame = { ...incoming, seq, request_id: `c-${seq}`, kind: "request", payload: validateInput(method, input) as unknown as Record<string, Json> };
        const result = new Promise<unknown>((resolve, reject) => {
          const timer = setTimeout(() => fence(new ProtocolError("native service acknowledgement expired")), Math.max(1, incoming.deadline_at - Date.now()));
          pipes.track(`service-${request.request_id}`, () => { clearTimeout(timer); });
          pending.set(request.request_id, { frame: request, resolve, reject });
        });
        // Attach a rejection observer before asynchronous pipe publication.
        void result.catch(() => {});
        try { await send(request); return await result; }
        catch (error) { fence(new ProtocolError("native service pipe failed")); throw error; }
      },
    };
    scopes.set(incoming.request_id, scope);
    scope.done = (async () => {
      try {
        const result = await composition.invoke(incoming.method, validateInput(incoming.method, incoming.payload), client);
        if (!client.isActive()) throw new ProtocolError("service response scope expired");
        await response(incoming, incoming.method, result as unknown as Record<string, Json>);
      } finally { scope.active = false; scopes.delete(incoming.request_id); }
    })();
    void scope.done.catch(error => fence(error instanceof Error ? error : new ProtocolError("service failed")));
    return Promise.resolve();
  };
  try {
    for await (const frame of readFrames(process.stdin)) {
      if (failure || frame.seq !== inboundSeq + 1 || frame.deadline_at <= Date.now() || frame.deadline_at - Date.now() > CONTROL_TIMEOUT_MS || frame.composition_digest !== identity.composition_digest || frame.package_digest !== identity.package_digest) throw new ProtocolError("frame identity or deadline mismatch");
      if (nonce === undefined) {
        if (frame.kind !== "request" || frame.method !== "bootstrap.hello" || frame.seq !== 1) throw new ProtocolError("hello required");
        nonce = frame.boot_nonce;
      } else if (frame.boot_nonce !== nonce || frame.method === "bootstrap.hello") throw new ProtocolError("stale boot or repeated hello");
      inboundSeq = frame.seq;
      if (frame.kind === "response") {
        const item = pending.get(frame.request_id);
        if (!item || !isServiceMethod(frame.method)) throw new ProtocolError("unsolicited service response");
        const original = item.frame;
        if (frame.method !== original.method || frame.invocation_ref !== original.invocation_ref || frame.composition_epoch !== original.composition_epoch || frame.deadline_at !== original.deadline_at) throw new ProtocolError("service response binding mismatch");
        pending.delete(frame.request_id);
        await pipes.release(`service-${frame.request_id}`);
        item.resolve(validateResult(frame.method, frame.payload));
        continue;
      }
      if (frame.request_id !== `r-${frame.seq}`) throw new ProtocolError("request identity mismatch");
      if (isServiceMethod(frame.method)) { await invoke(frame as Frame & {method: ServiceMethod}); continue; }
      if (frame.method === "bootstrap.hello") {
        await composition.start();
        await response(frame, "runtime.ready", { state: "ready", plugins: composition.states() as unknown as Json });
      } else if (frame.method === "runtime.status") {
        composition.assertReady();
        await response(frame, frame.method, { state: quiescing ? "quiescing" : "ready", plugins: composition.states() as unknown as Json, resources_remaining: composition.resources.remaining });
      } else if (frame.method === "runtime.quiesce") {
        quiescing = true;
        for (const scope of scopes.values()) scope.active = false;
        await response(frame, frame.method, { state: "quiescing" });
      } else if (frame.method === "runtime.shutdown") {
        quiescing = true;
        if (pending.size || scopes.size) throw new ProtocolError("service cleanup not quiescent");
        const cleanup = await composition.dispose();
        await response(frame, frame.method, { state: "stopped", ...cleanup });
        shutDown = true; break;
      } else if (frame.method === "invocation.cancel") {
        for (const scope of scopes.values()) if (scope.frame.invocation_ref === frame.invocation_ref) scope.active = false;
        // Local fencing is not positive physical cancellation of native work.
        await response(frame, frame.method, { cancelled: false });
      } else throw new ProtocolError("method unavailable");
    }
  } finally {
    for (const scope of scopes.values()) scope.active = false;
    for (const item of pending.values()) item.reject(new ProtocolError("owned pipe closed"));
    pending.clear();
    await Promise.allSettled([...scopes.values()].map(scope => scope.done));
    if (!shutDown) await composition.dispose();
    await pipes.dispose();
    if (!pipes.clean || !composition.resources.clean) throw new ProtocolError("cleanup unknown");
  }
}
main().catch(() => {
  process.stderr.write("Cordis runtime fenced: protocol, package, or lifecycle failure\n");
  process.exitCode = 1; process.stdin.destroy();
});
