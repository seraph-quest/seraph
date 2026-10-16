/** Finite wire schemas. No policy, state owner, dispatch or transport lives here. */
import { decodeJson, ProtocolError } from '../protocol.js';

export interface Schema<T> { parse(value: unknown): T; }
export type Infer<S> = S extends Schema<infer T> ? T : never;
export const object = <S extends Record<string, Schema<unknown>>>(fields: S): Schema<{ [K in keyof S]: Infer<S[K]> }> => ({
  parse(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ProtocolError('invalid service object');
    const record = value as Record<string, unknown>;
    const keys = Object.keys(fields);
    if (Object.keys(record).length !== keys.length || keys.some(key => !Object.hasOwn(record, key))) throw new ProtocolError('unknown or missing service field');
    const result: Record<string, unknown> = {};
    for (const key of keys) result[key] = fields[key]!.parse(record[key]);
    return result as { [K in keyof S]: Infer<S[K]> };
  },
});
export const text = (maxBytes: number, minBytes = 0): Schema<string> => ({ parse(value) {
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') < minBytes || Buffer.byteLength(value, 'utf8') > maxBytes || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)) throw new ProtocolError('invalid service text');
  return value;
} });
export const ref: Schema<string> = { parse(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value)) throw new ProtocolError('invalid canonical reference');
  return value;
} };
export const digest: Schema<string> = { parse(value) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) throw new ProtocolError('invalid service digest');
  return value;
} };
export const number = (min = 0, max = Number.MAX_SAFE_INTEGER): Schema<number> => ({ parse(value) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) throw new ProtocolError('invalid service integer');
  return value;
} });
export const boolean: Schema<boolean> = { parse(value) { if (typeof value !== 'boolean') throw new ProtocolError('invalid service boolean'); return value; } };
export const literal = <const T extends string>(...values: T[]): Schema<T> => ({ parse(value) {
  if (typeof value !== 'string' || !values.includes(value as T)) throw new ProtocolError('invalid service enum');
  return value as T;
} });
export const nullable = <T>(schema: Schema<T>): Schema<T | null> => ({ parse(value) { return value === null ? null : schema.parse(value); } });
export const array = <T>(schema: Schema<T>, max: number): Schema<T[]> => ({ parse(value) {
  if (!Array.isArray(value) || value.length > max) throw new ProtocolError('invalid service array');
  return value.map(item => schema.parse(item));
} });
export function bounded(value: unknown): unknown {
  // Reuse raw frame complexity/UTF-8/duplicate-key bounds on both directions.
  return decodeJson(Buffer.from(JSON.stringify(value), 'utf8'));
}
