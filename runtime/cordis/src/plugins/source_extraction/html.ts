/** Port of backend/src/guardian/source_watch.py ``normalize_source_text``.
 *
 * Pure text transformation with Python's exact whitespace set, regex order and
 * ``html.unescape`` rules; a parity test compares both owners on one corpus.
 */
import { HTML5_ENTITIES } from './html-entities.js';

// Python str.isspace() / re ``\s`` for str patterns.
const WS = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
// Python re.I folding for these tag letters (computed from CPython's re), as
// explicit classes: JS /i folds differently (e.g. it does not fold ı/İ to i).
const FOLD: Readonly<Record<string, string>> = Object.freeze({ i: '[iI\u0130\u0131]', s: '[sS\u017f]' });
const folded = (word: string) => Array.from(word, c => /[a-z]/.test(c) ? (FOLD[c] ?? `[${c}${c.toUpperCase()}]`) : c).join('');
// Python ``\b`` after a word character: the next character is not alnum/_.
const SCRIPT_OPEN = new RegExp(`<(${['script', 'style', 'noscript'].map(folded).join('|')})(?![\\p{L}\\p{N}_])`, 'gu');
const BLOCK_CLOSE = new RegExp(`</(${['p', 'div', 'li', 'h[1-6]', 'br', 'tr', 'section', 'article'].map(folded).join('|')})>`, 'gu');
const CHARREF = /&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)/gu;
const RUNS = new RegExp(`[${WS}]+`, 'u');

// Linear equivalents of ``<!--.*?-->``, ``<(script|style|noscript)\b[^>]*>.*?</\1\s*>``
// and ``<[^>]+>`` (identical output to the Python owner): an unclosed construct is
// scanned once instead of once per opening, so adversarial pages cannot stall the child.
function stripComments(value: string): string {
  let out = '', cursor = 0, start: number;
  while ((start = value.indexOf('<!--', cursor)) >= 0) {
    const end = value.indexOf('-->', start + 4);
    if (end < 0) break; // No later opening can close either.
    out += value.slice(cursor, start); cursor = end + 3;
  }
  return out + value.slice(cursor);
}
function stripScripts(value: string): string {
  let out = '', cursor = 0;
  const closing = new Map<string, [number, number]>();
  SCRIPT_OPEN.lastIndex = 0;
  let opening: RegExpExecArray | null;
  while ((opening = SCRIPT_OPEN.exec(value)) !== null) {
    const start = opening.index, nameEnd = value.indexOf('>', start + opening[0].length);
    if (nameEnd < 0) break; // ``[^>]*>`` can never complete for this or any later opening.
    const name = opening[1]!, key = name.toLowerCase();
    let cached = closing.get(key);
    if (cached === undefined || (cached[0] !== -1 && cached[0] < nameEnd + 1)) {
      // Python's ``\1`` under re.I compares lower() per character (so ``ſ`` is not ``s``).
      const letters = Array.from(name, c => /^[A-Za-z]$/.test(c) ? `[${c.toLowerCase()}${c.toUpperCase()}]` : c).join('');
      const pattern = new RegExp(`</${letters}[${WS}]*>`, 'gu');
      pattern.lastIndex = nameEnd + 1;
      const found = pattern.exec(value);
      cached = found ? [found.index, found.index + found[0].length] : [-1, -1];
      closing.set(key, cached);
    }
    if (cached[0] < 0) { SCRIPT_OPEN.lastIndex = start + 1; continue; }
    out += value.slice(cursor, start) + '\n';
    cursor = SCRIPT_OPEN.lastIndex = cached[1];
  }
  return out + value.slice(cursor);
}
function stripTags(value: string): string {
  let out = '', cursor = 0, position = 0, start: number;
  while ((start = value.indexOf('<', position)) >= 0) {
    const end = value.indexOf('>', start + 1);
    if (end < 0) break;
    if (end === start + 1) { position = start + 1; continue; } // ``<>`` needs one character.
    out += value.slice(cursor, start); cursor = position = end + 1;
  }
  return out + value.slice(cursor);
}

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
  // Joined non-empty runs have no edge whitespace, so Python's final strip() is a no-op.
  return value.split('\n').map(line => line.split(RUNS).filter(Boolean).join(' ')).filter(Boolean).join('\n');
}

export function normalizeSourceText(content: string, htmlContent: boolean): string {
  let value = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  if (htmlContent) {
    value = stripComments(value);
    value = stripScripts(value);
    value = value.replace(BLOCK_CLOSE, '\n');
    value = stripTags(value);
    value = unescapeHtml(value);
  }
  return normalizeLines(value);
}
