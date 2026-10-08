/** Closed, bounded A1.1 protocol; it deliberately admits no service methods. */
import { TextDecoder } from 'node:util';
import type { Readable, Writable } from 'node:stream';
import { Resources } from './resources.js';

export const MAX_FRAME = 1_048_576;
export const CONTROL_TIMEOUT_MS = 5_000;
export const MAX_PENDING = 32;
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Method = 'bootstrap.hello' | 'runtime.ready' | 'runtime.status' | 'runtime.quiesce' | 'runtime.shutdown' | 'invocation.cancel';
export interface Frame {
  protocol: 1; boot_nonce: string; request_id: string; seq: number;
  kind: 'request' | 'response'; method: Method; invocation_ref: string | null;
  composition_epoch: null; composition_digest: string; package_digest: string;
  deadline_at: number; payload: Record<string, Json>;
}
export class ProtocolError extends Error {}
const fields = ['protocol', 'boot_nonce', 'request_id', 'seq', 'kind', 'method', 'invocation_ref', 'composition_epoch', 'composition_digest', 'package_digest', 'deadline_at', 'payload'];
const methods = new Set<string>(['bootstrap.hello', 'runtime.ready', 'runtime.status', 'runtime.quiesce', 'runtime.shutdown', 'invocation.cancel']);
const hex64 = /^[0-9a-f]{64}$/;
const token = /^[A-Za-z0-9_.:-]{1,128}$/;

export function integer(value: Json | undefined, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) throw new ProtocolError('invalid integer');
  return value;
}
export function closed(value: Json | undefined, keys: string[]): Record<string, Json> {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) throw new ProtocolError('unknown or missing fields');
  return value;
}

/** JSON.parse alone loses duplicate keys and whether an integer used float syntax. */
export function decodeJson(data: Uint8Array): Json {
  if (data.length < 1 || data.length > MAX_FRAME) throw new ProtocolError('frame size exceeded');
  let source: string;
  try { source = new TextDecoder('utf-8', { fatal: true }).decode(data); }
  catch { throw new ProtocolError('invalid UTF-8'); }
  let offset = 0;
  let nodes = 0;
  const space = () => { while (/[ \t\r\n]/.test(source[offset] ?? '\0')) offset++; };
  const string = (): string => {
    const start = offset++;
    while (offset < source.length) {
      const char = source[offset++];
      if (char === '\\') { offset++; continue; }
      if (char === '"') {
        try { return JSON.parse(source.slice(start, offset)) as string; }
        catch { throw new ProtocolError('invalid JSON string'); }
      }
    }
    throw new ProtocolError('incomplete JSON string');
  };
  const parse = (depth: number, key?: string): Json => {
    if (depth > 16 || ++nodes > 4096) throw new ProtocolError('JSON complexity exceeded');
    space();
    const char = source[offset];
    if (char === '"') return string();
    if (char === '{') {
      offset++; space();
      const result: Record<string, Json> = Object.create(null) as Record<string, Json>;
      if (source[offset] === '}') { offset++; return result; }
      while (true) {
        space();
        if (source[offset] !== '"') throw new ProtocolError('invalid object key');
        const name = parse(depth + 1);
        if (typeof name !== 'string' || Object.hasOwn(result, name)) throw new ProtocolError('duplicate JSON key');
        space();
        if (source[offset++] !== ':') throw new ProtocolError('missing colon');
        result[name] = parse(depth + 1, name);
        space();
        const delimiter = source[offset++];
        if (delimiter === '}') return result;
        if (delimiter !== ',') throw new ProtocolError('invalid object delimiter');
      }
    }
    if (char === '[') {
      offset++; space();
      const result: Json[] = [];
      if (source[offset] === ']') { offset++; return result; }
      while (true) {
        result.push(parse(depth + 1)); space();
        const delimiter = source[offset++];
        if (delimiter === ']') return result;
        if (delimiter !== ',') throw new ProtocolError('invalid array delimiter');
      }
    }
    for (const [literal, value] of [['true', true], ['false', false], ['null', null]] as const) {
      if (source.startsWith(literal, offset)) { offset += literal.length; return value; }
    }
    const number = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(source.slice(offset))?.[0];
    if (!number) throw new ProtocolError('invalid JSON token');
    offset += number.length;
    const value = Number(number);
    if (!Number.isFinite(value)) throw new ProtocolError('nonfinite number');
    if (depth === 2 && key && ['protocol', 'seq', 'deadline_at', 'composition_epoch'].includes(key) && !/^-?(?:0|[1-9][0-9]*)$/.test(number)) throw new ProtocolError('integer used float syntax');
    return value;
  };
  const result = parse(1); space();
  if (offset !== source.length) throw new ProtocolError('trailing JSON');
  return result;
}

function plugins(value: Json | undefined): void {
  if (!Array.isArray(value) || value.length > 64) throw new ProtocolError('invalid plugins');
  const seen = new Set<string>();
  for (const item of value) {
    const plugin = closed(item, ['id', 'state', 'reason']);
    if (typeof plugin.id !== 'string' || plugin.id.length < 1 || plugin.id.length > 128 || seen.has(plugin.id)) throw new ProtocolError('invalid plugin identity');
    seen.add(plugin.id);
    if (typeof plugin.state !== 'string' || !['ready', 'blocked', 'stopped'].includes(plugin.state) || (plugin.reason !== null && (typeof plugin.reason !== 'string' || plugin.reason.length > 256))) throw new ProtocolError('invalid plugin state');
  }
}
export function validateFrame(value: Json): Frame {
  const frame = closed(value, fields);
  integer(frame.protocol, 1, 1); integer(frame.seq, 1); integer(frame.deadline_at, 1);
  for (const key of ['boot_nonce', 'composition_digest', 'package_digest']) if (typeof frame[key] !== 'string' || !hex64.test(frame[key])) throw new ProtocolError('invalid boot identity');
  if (typeof frame.request_id !== 'string' || !token.test(frame.request_id)) throw new ProtocolError('invalid request identity');
  if (typeof frame.method !== 'string' || !methods.has(frame.method) || (frame.kind !== 'request' && frame.kind !== 'response')) throw new ProtocolError('unknown method or kind');
  if (frame.composition_epoch !== null) throw new ProtocolError('A1.1 controls have no ownership epoch');
  if (frame.method === 'invocation.cancel') {
    if (typeof frame.invocation_ref !== 'string' || !token.test(frame.invocation_ref)) throw new ProtocolError('invalid invocation reference');
  } else if (frame.invocation_ref !== null) throw new ProtocolError('lifecycle controls have no invocation');
  const payload = frame.payload;
  if (frame.kind === 'request') {
    if (frame.method === 'runtime.ready') throw new ProtocolError('ready is response only');
    closed(payload, []);
  } else if (frame.method === 'runtime.ready') {
    const result = closed(payload, ['state', 'plugins']);
    if (result.state !== 'ready') throw new ProtocolError('invalid readiness');
    plugins(result.plugins);
  } else if (frame.method === 'runtime.status') {
    const result = closed(payload, ['state', 'plugins', 'resources_remaining']);
    if (result.state !== 'ready' && result.state !== 'quiescing') throw new ProtocolError('invalid runtime state');
    plugins(result.plugins); integer(result.resources_remaining, 0, 4096);
  } else if (frame.method === 'runtime.quiesce') {
    if (closed(payload, ['state']).state !== 'quiescing') throw new ProtocolError('invalid quiescence');
  } else if (frame.method === 'runtime.shutdown') {
    const result = closed(payload, ['state', 'resources_remaining', 'cordis_disposal']);
    if (result.state !== 'stopped' || (result.cordis_disposal !== 'confirmed' && result.cordis_disposal !== 'unconfirmed')) throw new ProtocolError('invalid shutdown');
    integer(result.resources_remaining, 0, 4096);
  } else if (frame.method === 'invocation.cancel') {
    if (typeof closed(payload, ['cancelled']).cancelled !== 'boolean') throw new ProtocolError('invalid cancellation');
  } else throw new ProtocolError('hello is request only');
  return frame as unknown as Frame;
}
export function encodeFrame(frame: Frame): Buffer {
  validateFrame(frame as unknown as Json);
  const body = Buffer.from(JSON.stringify(frame));
  // Apply complexity limits to outgoing frames too.
  decodeJson(body);
  const header = Buffer.alloc(4); header.writeUInt32BE(body.length);
  return Buffer.concat([header, body]);
}
export async function* readFrames(stream: Readable): AsyncGenerator<Frame> {
  let buffered = Buffer.alloc(0);
  let length: number | undefined;
  for await (const raw of stream) {
    const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw as Uint8Array);
    buffered = Buffer.concat([buffered, chunk]);
    while (true) {
      if (length === undefined) {
        if (buffered.length < 4) break;
        length = buffered.readUInt32BE(); buffered = buffered.subarray(4);
        if (length < 1 || length > MAX_FRAME) throw new ProtocolError('frame size exceeded');
      }
      if (buffered.length < length) break;
      const body = buffered.subarray(0, length); buffered = buffered.subarray(length); length = undefined;
      yield validateFrame(decodeJson(body));
    }
    if (buffered.length > MAX_FRAME) throw new ProtocolError('frame buffer exceeded');
  }
  throw new ProtocolError('owned pipe lost or incomplete frame');
}
export async function writeFrame(stream: Writable, frame: Frame, resources: Resources): Promise<void> {
  const remaining = Math.min(CONTROL_TIMEOUT_MS, frame.deadline_at - Date.now());
  if (remaining <= 0) throw new ProtocolError('deadline expired');
  const data = encodeFrame(frame);
  let timer: NodeJS.Timeout | undefined;
  const id = `pipe-write-${frame.seq}`;
  resources.track(id, () => { if (timer) clearTimeout(timer); });
  try {
    await new Promise<void>((resolve, reject) => {
      timer = setTimeout(() => reject(new ProtocolError('pipe write timeout')), remaining);
      stream.write(data, error => { if (error) reject(error); else resolve(); });
    });
  } finally { await resources.release(id); }
}
