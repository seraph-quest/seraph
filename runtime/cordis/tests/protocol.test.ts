import assert from 'node:assert/strict';
import test from 'node:test';
import { Readable } from 'node:stream';
import { decodeJson, encodeFrame, readFrames, validateFrame, type Frame } from '../src/protocol.js';
import { Resources } from '../src/resources.js';
import { Composition, validateProfile } from '../src/composition.js';

const frame: Frame = { protocol: 1, boot_nonce: 'a'.repeat(64), request_id: 'r-1', seq: 1, kind: 'request', method: 'bootstrap.hello', invocation_ref: null, composition_epoch: null, composition_digest: 'b'.repeat(64), package_digest: 'c'.repeat(64), deadline_at: 2_000_000_000_000, payload: {} };
test('raw length-prefix framing survives fragmented input without delimiters', async () => {
  const wire = encodeFrame(frame);
  assert.equal(wire.readUInt32BE(0), wire.length - 4);
  const input = Readable.from([wire.subarray(0, 1), wire.subarray(1, 5), wire.subarray(5)]);
  const iterator = readFrames(input);
  const result = await iterator.next();
  assert.equal(result.value?.boot_nonce, frame.boot_nonce);
  await assert.rejects(iterator.next(), /pipe lost/);
});
test('rejects duplicate escaped keys, malformed UTF8, nonfinite, depth and node attacks', () => {
  for (const source of ['{"x":1,"\\u0078":2}', '1e999', '{"x":NaN}', '['.repeat(17) + '0' + ']'.repeat(17), JSON.stringify(Array(4096).fill(0))]) assert.throws(() => decodeJson(Buffer.from(source)));
  assert.throws(() => decodeJson(Buffer.from([0xff])));
});
test('integer fields reject strings, float syntax, unsafe values and unknown fields/methods', () => {
  for (const bad of [{ ...frame, seq: '1' }, { ...frame, seq: 2 ** 53 }, { ...frame, method: 'tools.run' }, { ...frame, extra: 1 }, { ...frame, composition_epoch: 1 }, { ...frame, payload: { path: '/tmp' } }]) assert.throws(() => validateFrame(bad as never));
  assert.throws(() => decodeJson(Buffer.from(JSON.stringify(frame).replace('"seq":1', '"seq":1.0'))));
  assert.throws(() => decodeJson(Buffer.from(JSON.stringify(frame).replace('"seq":1', '"seq":1e0'))));
});
test('oversized lengths and incomplete frames never yield a dispatch', async () => {
  const oversized = Buffer.alloc(4); oversized.writeUInt32BE(1_048_577);
  for (const wire of [oversized, Buffer.from([0, 0, 0, 5, 123]), Buffer.from([0, 0, 0, 0])]) {
    let dispatched = 0;
    await assert.rejects(async () => { for await (const _frame of readFrames(Readable.from([wire]))) dispatched++; });
    assert.equal(dispatched, 0);
  }
});
test('nested status and shutdown integers reject float, exponent and unsafe tokens', () => {
  for (const method of ['runtime.status', 'runtime.shutdown'] as const) {
    const payload = method === 'runtime.status'
      ? { state: 'ready', plugins: [], resources_remaining: 1 }
      : { state: 'stopped', resources_remaining: 1, cordis_disposal: 'confirmed' };
    const source = JSON.stringify({ ...frame, kind: 'response', method, payload });
    assert.equal(validateFrame(decodeJson(Buffer.from(source))).payload.resources_remaining, 1);
    for (const token of ['1.0', '1e0', '9007199254740993']) {
      const malformed = source.replace('"resources_remaining":1', `"resources_remaining":${token}`);
      assert.throws(() => validateFrame(decodeJson(Buffer.from(malformed))), /integer/, `${method}: ${token}`);
    }
  }
});
test('cleanup errors retain owned resources instead of falsely passing disposal', async () => {
  const resources = new Resources();
  resources.track('owned', () => { throw Error('cleanup unavailable'); });
  await resources.dispose();
  assert.equal(resources.clean, false);
  assert.equal(resources.remaining, 1);
});
test('real stock Cordis host plugin starts and explicitly disposes its service', async () => {
  const profile = validateProfile(decodeJson(Buffer.from('{"protocol":1,"profile_id":"seraph-cordis-bootstrap-v1","plugins":[{"id":"seraph.host-lifecycle@1.0.0","required":true,"dependencies":[],"config":{}}]}')));
  const composition = new Composition(profile);
  await composition.start();
  assert.equal(composition.states()[0]?.state, 'ready');
  assert.equal(composition.context.hostLifecycle.ready, true);
  assert.deepEqual(await composition.dispose(), { resources_remaining: 0, cordis_disposal: 'confirmed' });
  assert.equal(composition.context.get('hostLifecycle'), undefined);
  assert.throws(() => composition.assertReady(), /unavailable/);
});
test('required service loss fences readiness and invalid literal config never starts', async () => {
  const profile = validateProfile(decodeJson(Buffer.from('{"protocol":1,"profile_id":"seraph-cordis-bootstrap-v1","plugins":[{"id":"seraph.host-lifecycle@1.0.0","required":true,"dependencies":[],"config":{}}]}')));
  const composition = new Composition(profile);
  await composition.start();
  const fibers = [...composition.context.registry.values()].flatMap(runtime => [...runtime.fibers]);
  await Promise.all(fibers.map(fiber => fiber.dispose()));
  assert.throws(() => composition.assertReady(), /unavailable/);
  await composition.dispose();
  assert.throws(() => validateProfile({ ...profile, plugins: [{ ...profile.plugins[0], config: { module: 'arbitrary' } }] } as never));
});
