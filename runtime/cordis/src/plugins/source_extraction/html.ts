/** Port of backend/src/guardian/source_watch.py ``normalize_source_text``.
 *
 * Pure text transformation with Python's exact whitespace set, regex order and
 * ``html.unescape`` rules; a parity test compares both owners on one corpus.
 */
import { HTML5_ENTITIES } from './html-entities.js';

// Python str.isspace() / re ``\s`` for str patterns.
const WS = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const COMMENT = /<!--[\s\S]*?-->/gu;
// Python ``\b`` after a word character: the next character is not alnum/_.
const SCRIPT = new RegExp(`<(script|style|noscript)(?![\\p{L}\\p{N}_])[^>]*>[\\s\\S]*?</\\1[${WS}]*>`, 'giu');
const BLOCK_CLOSE = /<\/(p|div|li|h[1-6]|br|tr|section|article)>/giu;
const TAG = /<[^>]+>/gu;
const CHARREF = /&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)/gu;
const RUNS = new RegExp(`[${WS}]+`, 'u');
const EDGE = new RegExp(`^[${WS}]+|[${WS}]+$`, 'gu');

const INVALID_CHARREFS: Readonly<Record<number, string>> = Object.freeze({
  0x00: '�', 0x0d: '\r', 0x80: '€', 0x81: '\x81', 0x82: '‚', 0x83: 'ƒ', 0x84: '„',
  0x85: '…', 0x86: '†', 0x87: '‡', 0x88: 'ˆ', 0x89: '‰', 0x8a: 'Š', 0x8b: '‹',
  0x8c: 'Œ', 0x8d: '\x8d', 0x8e: 'Ž', 0x8f: '\x8f', 0x90: '\x90', 0x91: '‘', 0x92: '’',
  0x93: '“', 0x94: '”', 0x95: '•', 0x96: '–', 0x97: '—', 0x98: '˜', 0x99: '™',
  0x9a: 'š', 0x9b: '›', 0x9c: 'œ', 0x9d: '\x9d', 0x9e: 'ž', 0x9f: 'Ÿ',
});

function invalidCodepoint(value: number): boolean {
  return (value >= 0x1 && value <= 0x8) || value === 0xb || (value >= 0xe && value <= 0x1f)
    || (value >= 0x7f && value <= 0x9f) || (value >= 0xfdd0 && value <= 0xfdef) || (value & 0xfffe) === 0xfffe;
}

function numeric(digits: string, radix: number): string {
  const trimmed = digits.replace(/^0+/, '');
  // Beyond U+10FFFF regardless of length; avoid unsafe integer parsing.
  const value = trimmed.length > (radix === 16 ? 6 : 7) ? Number.POSITIVE_INFINITY : trimmed ? parseInt(trimmed, radix) : 0;
  if (Object.hasOwn(INVALID_CHARREFS, value)) return INVALID_CHARREFS[value]!;
  if ((value >= 0xd800 && value <= 0xdfff) || value > 0x10ffff) return '�';
  if (invalidCodepoint(value)) return '';
  return String.fromCodePoint(value);
}

/** Python ``html.unescape``: numeric rules plus the WHATWG named table. */
export function unescapeHtml(value: string): string {
  if (!value.includes('&')) return value;
  return value.replace(CHARREF, (_match, body: string) => {
    if (body.startsWith('#')) {
      const hex = body[1] === 'x' || body[1] === 'X';
      return numeric(body.slice(hex ? 2 : 1).replace(/;$/, ''), hex ? 16 : 10);
    }
    if (Object.hasOwn(HTML5_ENTITIES, body)) return HTML5_ENTITIES[body]!;
    // Longest legacy prefix, as defined by the standard and Python.
    const points = Array.from(body);
    for (let size = points.length - 1; size > 1; size--) {
      const prefix = points.slice(0, size).join('');
      if (Object.hasOwn(HTML5_ENTITIES, prefix)) return HTML5_ENTITIES[prefix]! + points.slice(size).join('');
    }
    return '&' + body;
  });
}

/** Collapse whitespace runs per line and drop empty lines (Python ``str.split``). */
export function normalizeLines(value: string): string {
  const lines = value.split('\n').map(line => line.replace(EDGE, '').split(RUNS).filter(Boolean).join(' '));
  return lines.filter(Boolean).join('\n').replace(EDGE, '');
}

export function normalizeSourceText(content: string, htmlContent: boolean): string {
  let value = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  if (htmlContent) {
    value = value.replace(COMMENT, '');
    value = value.replace(SCRIPT, '\n');
    value = value.replace(BLOCK_CLOSE, '\n');
    value = value.replace(TAG, '');
    value = unescapeHtml(value);
  }
  return normalizeLines(value);
}
