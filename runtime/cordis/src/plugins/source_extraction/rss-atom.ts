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

interface Element { name: string; attributes: Record<string, string>; children: Element[]; text: string; }

const NAME = /^[A-Za-z_][A-Za-z0-9_.:-]{0,127}/;
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
  if (Buffer.byteLength(source, 'utf8') > FEED_LIMITS.maxInputBytes) throw new FeedRejected('source_output_oversize');
  for (const char of source) if (!xmlChar(char.codePointAt(0)!)) throw new FeedRejected('source_feed_malformed');
  let offset = 0, elements = 0;
  const declaration = /^<\?xml(\s[^?]*)?\?>/.exec(source);
  if (declaration) {
    const attributes = declaration[1] ?? '';
    const encoding = /\bencoding\s*=\s*["']([^"']*)["']/.exec(attributes);
    if (!/\bversion\s*=\s*["']1\.[0-9]["']/.test(attributes) || (encoding && !/^utf-8$/i.test(encoding[1]!))) throw new FeedRejected('source_feed_malformed');
    offset = declaration[0].length;
  }
  const stack: Element[] = [];
  let root: Element | undefined;
  const append = (text: string) => {
    const current = stack[stack.length - 1];
    if (current) {
      current.text += text;
      if (Buffer.byteLength(current.text, 'utf8') > FEED_LIMITS.maxInputBytes) throw new FeedRejected('source_output_oversize');
    } else if (text.trim()) throw new FeedRejected('source_feed_malformed');
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
      if (end < 0 || /^<\?xml(\s|\?)/i.test(source.slice(offset, offset + 6))) throw new FeedRejected('source_feed_malformed');
      offset = end + 2;
    } else if (source.startsWith('</', offset)) {
      const name = NAME.exec(source.slice(offset + 2));
      const current = stack.pop();
      if (!name || !current || current.name !== name[0]) throw new FeedRejected('source_feed_malformed');
      offset += 2 + name[0].length;
      while (/\s/.test(source[offset] ?? '')) offset++;
      if (source[offset++] !== '>') throw new FeedRejected('source_feed_malformed');
    } else {
      const name = NAME.exec(source.slice(offset + 1));
      if (!name) throw new FeedRejected('source_feed_malformed');
      if (++elements > FEED_LIMITS.maxElements || stack.length >= FEED_LIMITS.maxDepth) throw new FeedRejected('source_output_oversize');
      const element: Element = { name: name[0], attributes: Object.create(null) as Record<string, string>, children: [], text: '' };
      offset += 1 + name[0].length;
      let selfClosing = false;
      while (true) {
        const spaced = /^\s*/.exec(source.slice(offset))![0].length;
        offset += spaced;
        if (source.startsWith('/>', offset)) { selfClosing = true; offset += 2; break; }
        if (source[offset] === '>') { offset++; break; }
        const attribute = /^([A-Za-z_][A-Za-z0-9_.:-]{0,127})\s*=\s*("([^"]*)"|'([^']*)')/.exec(source.slice(offset));
        if (!spaced || !attribute || Object.hasOwn(element.attributes, attribute[1]!)) throw new FeedRejected('source_feed_malformed');
        element.attributes[attribute[1]!] = decodeEntities(attribute[3] ?? attribute[4] ?? '');
        offset += attribute[0].length;
      }
      const parent = stack[stack.length - 1];
      if (parent) parent.children.push(element);
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
const field = (value: string | undefined, html = false): string => {
  const text = html ? normalizeSourceText(value ?? '', true) : normalizeLines(value ?? '');
  if (Buffer.byteLength(text, 'utf8') > FEED_LIMITS.maxFieldBytes) throw new FeedRejected('source_output_oversize');
  return text.replace(/\n/g, ' ');
};

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
      ['Link', field(link)],
      ['Published', field(child(entry, atom ? 'updated' : 'pubDate')?.text)],
      ['Summary', field((atom ? (child(entry, 'summary') ?? child(entry, 'content')) : child(entry, 'description'))?.text, true)],
    ];
    for (const [label, value] of values) if (value) lines.push(`${label}: ${value}`);
  }
  return normalizeLines(lines.join('\n'));
}
