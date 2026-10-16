/** Bounded RSS 2.0 / Atom 1.0 reader. No DTD, external or custom entities.
 *
 * A hand-written tokenizer over an already UTF-8-decoded public snapshot: it
 * rejects DOCTYPE/ENTITY and every ``<!`` construct except comments and CDATA,
 * resolves only the five predefined entities and valid numeric references,
 * and enforces depth, element, item and byte caps. Links are returned as
 * inert text; nothing here fetches or follows them.
 */
import { normalizeLines, normalizeSourceText } from './html.js';

export const FEED_LIMITS = Object.freeze({ maxInputBytes: 262_144, maxDepth: 32, maxElements: 4096, maxItems: 64, maxFieldBytes: 4096 });

export class FeedRejected extends Error {
  constructor(readonly reason: 'source_feed_malformed' | 'source_feed_dtd_forbidden' | 'source_feed_entity_forbidden' | 'source_output_oversize') { super(reason); }
}

/** ``parts`` keeps text and child elements in document order (Atom xhtml content). */
interface Element { name: string; attributes: Record<string, string>; children: Element[]; text: string; parts: (string | Element)[]; }

const NAME = /^[A-Za-z_][A-Za-z0-9_.:-]{0,127}/;
// XML's S production: JS ``\s`` also matches U+FEFF, NBSP and other Unicode spaces.
const XML_SPACE = /[ \t\r\n]/;
const PREDEFINED: Readonly<Record<string, string>> = Object.freeze({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" });

function xmlChar(value: number): boolean {
  return value === 0x9 || value === 0xa || value === 0xd || (value >= 0x20 && value <= 0xd7ff)
    || (value >= 0xe000 && value <= 0xfffd) || (value >= 0x10000 && value <= 0x10ffff);
}

function decodeEntities(raw: string): string {
  if (raw.includes('<')) throw new FeedRejected('source_feed_malformed');
  return raw.replace(/&([^;&\s]{0,16});?/g, (match, name: string) => {
    if (!match.endsWith(';')) throw new FeedRejected('source_feed_malformed');
    if (Object.hasOwn(PREDEFINED, name)) return PREDEFINED[name]!;
    const numeric = /^#(?:x([0-9A-Fa-f]{1,6})|([0-9]{1,7}))$/.exec(name);
    if (!numeric) throw new FeedRejected('source_feed_entity_forbidden');
    const value = numeric[1] !== undefined ? parseInt(numeric[1], 16) : parseInt(numeric[2]!, 10);
    if (!xmlChar(value)) throw new FeedRejected('source_feed_malformed');
    return String.fromCodePoint(value);
  });
}

/** Parse one well-formed document into a bounded element tree. */
export function parseXml(source: string): Element {
  // One UTF-8 byte order mark may precede the declaration; a second one, or
  // any other leading character, still makes the declaration malformed.
  if (source.startsWith('\uFEFF')) source = source.slice(1);
  if (Buffer.byteLength(source, 'utf8') > FEED_LIMITS.maxInputBytes) throw new FeedRejected('source_output_oversize');
  for (const char of source) if (!xmlChar(char.codePointAt(0)!)) throw new FeedRejected('source_feed_malformed');
  let offset = 0, elements = 0;
  const declaration = /^<\?xml([ \t\r\n][^?]*)?\?>/.exec(source);
  if (declaration) {
    const attributes = declaration[1] ?? '';
    const encoding = /\bencoding[ \t\r\n]*=[ \t\r\n]*["']([^"']*)["']/.exec(attributes);
    if (!/\bversion[ \t\r\n]*=[ \t\r\n]*["']1\.[0-9]["']/.test(attributes) || (encoding && !/^utf-8$/i.test(encoding[1]!))) throw new FeedRejected('source_feed_malformed');
    offset = declaration[0].length;
  }
  const stack: Element[] = [];
  let root: Element | undefined;
  const append = (text: string) => {
    const current = stack[stack.length - 1];
    if (current) {
      current.text += text;
      current.parts.push(text);
      if (Buffer.byteLength(current.text, 'utf8') > FEED_LIMITS.maxInputBytes) throw new FeedRejected('source_output_oversize');
    // Outside the root only XML whitespace may appear: JS trim() would also
    // drop U+FEFF and NBSP, letting a second BOM or a leading NBSP through.
    } else if (/[^ \t\r\n]/.test(text)) throw new FeedRejected('source_feed_malformed');
  };
  while (offset < source.length) {
    const next = source.indexOf('<', offset);
    if (next < 0) { append(decodeEntities(source.slice(offset))); break; }
    if (next > offset) append(decodeEntities(source.slice(offset, next)));
    offset = next;
    if (source.startsWith('<!--', offset)) {
      const end = source.indexOf('-->', offset + 4);
      if (end < 0 || source.slice(offset + 4, end).includes('--')) throw new FeedRejected('source_feed_malformed');
      offset = end + 3;
    } else if (source.startsWith('<![CDATA[', offset)) {
      const end = source.indexOf(']]>', offset + 9);
      if (end < 0 || !stack.length) throw new FeedRejected('source_feed_malformed');
      append(source.slice(offset + 9, end));
      offset = end + 3;
    } else if (source.startsWith('<!', offset)) {
      // DOCTYPE, ENTITY, ELEMENT, ATTLIST and conditional sections never parse.
      throw new FeedRejected(/^<!(DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)/i.test(source.slice(offset, offset + 12)) ? 'source_feed_dtd_forbidden' : 'source_feed_malformed');
    } else if (source.startsWith('<?', offset)) {
      // Processing instructions (stylesheets) are inert; the declaration is only first.
      const end = source.indexOf('?>', offset + 2);
      // Only a target that is exactly ``xml`` (not ``xml-stylesheet``) is the declaration.
      if (end < 0 || /^<\?xml(?![A-Za-z0-9_.:-])/i.test(source.slice(offset, offset + 6))) throw new FeedRejected('source_feed_malformed');
      offset = end + 2;
    } else if (source.startsWith('</', offset)) {
      const name = NAME.exec(source.slice(offset + 2));
      const current = stack.pop();
      if (!name || !current || current.name !== name[0]) throw new FeedRejected('source_feed_malformed');
      offset += 2 + name[0].length;
      while (XML_SPACE.test(source[offset] ?? '')) offset++;
      if (source[offset++] !== '>') throw new FeedRejected('source_feed_malformed');
    } else {
      const name = NAME.exec(source.slice(offset + 1));
      if (!name) throw new FeedRejected('source_feed_malformed');
      if (++elements > FEED_LIMITS.maxElements || stack.length >= FEED_LIMITS.maxDepth) throw new FeedRejected('source_output_oversize');
      const element: Element = { name: name[0], attributes: Object.create(null) as Record<string, string>, children: [], text: '', parts: [] };
      offset += 1 + name[0].length;
      let selfClosing = false;
      while (true) {
        const spaced = /^[ \t\r\n]*/.exec(source.slice(offset))![0].length;
        offset += spaced;
        if (source.startsWith('/>', offset)) { selfClosing = true; offset += 2; break; }
        if (source[offset] === '>') { offset++; break; }
        const attribute = /^([A-Za-z_][A-Za-z0-9_.:-]{0,127})[ \t\r\n]*=[ \t\r\n]*("([^"]*)"|'([^']*)')/.exec(source.slice(offset));
        if (!spaced || !attribute || Object.hasOwn(element.attributes, attribute[1]!)) throw new FeedRejected('source_feed_malformed');
        element.attributes[attribute[1]!] = decodeEntities(attribute[3] ?? attribute[4] ?? '');
        offset += attribute[0].length;
      }
      const parent = stack[stack.length - 1];
      if (parent) { parent.children.push(element); parent.parts.push(element); }
      else if (root) throw new FeedRejected('source_feed_malformed');
      else root = element;
      if (!selfClosing) stack.push(element);
    }
  }
  if (!root || stack.length) throw new FeedRejected('source_feed_malformed');
  return root;
}

const local = (name: string) => name.slice(name.indexOf(':') + 1);
const child = (element: Element, name: string) => element.children.find(item => local(item.name) === name);
const ELLIPSIS = '\u2026';
/** Cut to at most ``maxFieldBytes`` UTF-8 bytes on a code point boundary. */
function truncateField(text: string): string {
  if (Buffer.byteLength(text, 'utf8') <= FEED_LIMITS.maxFieldBytes) return text;
  const budget = FEED_LIMITS.maxFieldBytes - Buffer.byteLength(ELLIPSIS, 'utf8');
  let out = '', size = 0;
  for (const char of text) {
    const bytes = Buffer.byteLength(char, 'utf8');
    if (size + bytes > budget) break;
    out += char; size += bytes;
  }
  return out.trimEnd() + ELLIPSIS;
}
const field = (value: string | undefined, html = false): string =>
  // A long field is cut rather than rejecting the whole feed.
  truncateField((html ? normalizeSourceText(value ?? '', true) : normalizeLines(value ?? '')).replace(/\n/g, ' '));
/** A link is either exact or absent: a cut URL could read as a different resource. */
function linkField(value: string | undefined): string {
  const text = normalizeLines(value ?? '').replace(/\n/g, ' ');
  return Buffer.byteLength(text, 'utf8') <= FEED_LIMITS.maxFieldBytes ? text : '';
}
// Block elements whose boundaries separate words in Atom xhtml content; script
// and style bodies are never content.
const XHTML_SKIPPED = new Set(['script', 'style']);
const XHTML_BLOCKS = new Set(['p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br', 'tr', 'td', 'th', 'section',
  'article', 'blockquote', 'pre', 'ul', 'ol', 'table', 'dt', 'dd']);
/** Document-order text of an element, with line breaks at xhtml block boundaries. */
function deepText(element: Element): string {
  return element.parts.map(part => {
    if (typeof part === 'string') return part;
    const name = local(part.name).toLowerCase();
    if (XHTML_SKIPPED.has(name)) return '\n';
    return XHTML_BLOCKS.has(name) ? `\n${deepText(part)}\n` : deepText(part);
  }).join('');
}
/** Atom text constructs: ``xhtml`` is inline markup, ``text`` is literal, ``html`` and RSS are escaped HTML. */
function summary(element: Element | undefined, atom: boolean): string {
  if (!element) return '';
  if (atom && element.attributes.type === 'xhtml') return field(deepText(element));
  if (atom && element.attributes.type === 'text') return field(element.text);
  return field(element.text, true);
}

/** Normalized text lines: feed title, then each item's title/link/date/summary. */
export function feedText(source: string): string {
  const root = parseXml(source);
  let title: string, entries: Element[], atom: boolean;
  if (local(root.name) === 'rss') {
    const channel = child(root, 'channel');
    if (!channel) throw new FeedRejected('source_feed_malformed');
    title = field(child(channel, 'title')?.text);
    entries = channel.children.filter(item => local(item.name) === 'item');
    atom = false;
  } else if (local(root.name) === 'feed') {
    title = field(child(root, 'title')?.text);
    entries = root.children.filter(item => local(item.name) === 'entry');
    atom = true;
  } else throw new FeedRejected('source_feed_malformed');
  if (entries.length > FEED_LIMITS.maxItems) throw new FeedRejected('source_output_oversize');
  const lines = title ? [`Feed: ${title}`] : [];
  for (const entry of entries) {
    const link = atom
      ? (entry.children.find(item => local(item.name) === 'link' && (item.attributes.rel ?? 'alternate') === 'alternate')?.attributes.href ?? '')
      : child(entry, 'link')?.text;
    const values: [string, string][] = [
      ['Item', field(child(entry, 'title')?.text)],
      ['Link', linkField(link)],
      ['Published', field(child(entry, atom ? 'updated' : 'pubDate')?.text)],
      ['Summary', summary(atom ? (child(entry, 'summary') ?? child(entry, 'content')) : child(entry, 'description'), atom)],
    ];
    for (const [label, value] of values) if (value) lines.push(`${label}: ${value}`);
  }
  return normalizeLines(lines.join('\n'));
}
