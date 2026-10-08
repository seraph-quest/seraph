/** Trusted child bootstrap: composition/lifecycle only, no policy or agent loop. */
import { Composition, packageIdentity } from './composition.js';
import { CONTROL_TIMEOUT_MS, ProtocolError, readFrames, writeFrame, type Frame, type Json, type Method } from './protocol.js';
import { Resources } from './resources.js';

async function main(): Promise<void> {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (!((major === 22 && (minor ?? 0) >= 12) || major === 24)) throw new ProtocolError('unsupported Node');
  const identity = packageIdentity();
  const composition = new Composition(identity.profile);
  const pipes = new Resources();
  pipes.track('owned-input', () => { process.stdin.destroy(); });
  let nonce: string | undefined;
  let inboundSeq = 0;
  let outboundSeq = 0;
  let quiescing = false;
  let shutDown = false;
  const response = async (request: Frame, method: Method, payload: Record<string, Json>) => {
    if (request.deadline_at <= Date.now()) throw new ProtocolError('deadline expired');
    await writeFrame(process.stdout, { ...request, seq: ++outboundSeq, kind: 'response', method, payload }, pipes);
  };
  try {
    for await (const frame of readFrames(process.stdin)) {
      // Deterministic request IDs bind uniqueness to monotonically increasing seq,
      // avoiding an unbounded lifetime replay-ID store.
      if (frame.kind !== 'request' || frame.seq !== inboundSeq + 1 || frame.request_id !== `r-${frame.seq}` || frame.deadline_at <= Date.now() || frame.deadline_at - Date.now() > CONTROL_TIMEOUT_MS || frame.composition_digest !== identity.composition_digest || frame.package_digest !== identity.package_digest) throw new ProtocolError('request identity or deadline mismatch');
      if (nonce === undefined) {
        if (frame.method !== 'bootstrap.hello' || frame.seq !== 1) throw new ProtocolError('hello required');
        nonce = frame.boot_nonce;
      } else if (frame.boot_nonce !== nonce || frame.method === 'bootstrap.hello') throw new ProtocolError('stale boot or repeated hello');
      inboundSeq = frame.seq;
      if (frame.method === 'bootstrap.hello') {
        await composition.start();
        await response(frame, 'runtime.ready', { state: 'ready', plugins: composition.states() as unknown as Json });
      } else if (frame.method === 'runtime.status') {
        composition.assertReady();
        await response(frame, frame.method, { state: quiescing ? 'quiescing' : 'ready', plugins: composition.states() as unknown as Json, resources_remaining: composition.resources.remaining });
      } else if (frame.method === 'runtime.quiesce') {
        quiescing = true;
        await response(frame, frame.method, { state: 'quiescing' });
      } else if (frame.method === 'runtime.shutdown') {
        quiescing = true;
        const cleanup = await composition.dispose();
        await response(frame, frame.method, { state: 'stopped', ...cleanup });
        shutDown = true;
        break;
      } else if (frame.method === 'invocation.cancel') {
        // No A1.1 service invocation exists. Never invent cancellation success.
        await response(frame, frame.method, { cancelled: false });
      } else throw new ProtocolError('method unavailable');
    }
  } finally {
    if (!shutDown) await composition.dispose();
    await pipes.dispose();
    if (!pipes.clean) throw new ProtocolError('pipe cleanup unknown');
  }
}

main().catch(() => {
  // Bounded, non-secret diagnostic. Never log application values or frame bytes.
  process.stderr.write('Cordis runtime fenced: protocol, package, or lifecycle failure\n');
  process.exitCode = 1;
  process.stdin.destroy();
});
