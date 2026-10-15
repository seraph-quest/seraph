import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Composition, validateProfile } from '../src/composition.js';
import { decodeJson } from '../src/protocol.js';
import type { ScopedRequestClient } from '../src/contracts/client.js';
import { normalizeSourceText, unescapeHtml } from '../src/plugins/source_extraction/html.js';
import { FEED_LIMITS, FeedRejected, feedText } from '../src/plugins/source_extraction/rss-atom.js';

const sha = (value: string) => createHash('sha256').update(value).digest('hex');
const profile = () => validateProfile(decodeJson(readFileSync(new URL('../../profile.json', import.meta.url))));
const rejects = (source: string, reason: string) => assert.throws(() => feedText(source), (error: unknown) => error instanceof FeedRejected && error.reason === reason);

const RSS = `<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="https://example.com/style.xsl"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
  <title>Release &amp; notes</title><atom:link href="https://example.com/feed" rel="self"/>
  <item><title>Version 2 ships</title><link>https://example.com/v2</link><pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate>
    <description><![CDATA[<p>Faster <b>sync</b>.</p><script>alert(1)</script>]]></description></item>
  <item><title>Version 1</title><link>https://example.com/v1?a=1&amp;b=2</link><description>&lt;p&gt;Initial&lt;/p&gt; &#x2014; done</description></item>
</channel></rss>`;
const ATOM = `<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title type="text">Atom log</title>
<entry><title>Entry one</title><link rel="alternate" href="https://example.com/e1"/><link rel="edit" href="https://example.com/x"/>
<updated>2026-10-14T00:00:00Z</updated><summary type="html">&lt;em&gt;Summary&lt;/em&gt; text</summary></entry></feed>`;

test('HTML/text normalization ports Python normalize_source_text rules', () => {
  assert.equal(normalizeSourceText('<html><!-- x --><style>p{}</style><h1>Title</h1><p>One&nbsp;&amp; two</p><script>bad()</script>three</html>', true),
    'Title\nOne & two\nthree');
  assert.equal(normalizeSourceText('  a \t b \r\n\r\n c　d ', false), 'a b\nc d');
  assert.equal(unescapeHtml('&copy;&amp &ampx &#128; &#x110000; &#1; &unknown; &#0065'), '©& &x € �  &unknown; A');
});

test('RSS and Atom produce bounded normalized lines; links stay inert text', () => {
  assert.equal(feedText(RSS), ['Feed: Release & notes', 'Item: Version 2 ships', 'Link: https://example.com/v2',
    'Published: Tue, 13 Oct 2026 10:00:00 GMT', 'Summary: Faster sync.', 'Item: Version 1', 'Link: https://example.com/v1?a=1&b=2',
    'Summary: Initial — done'].join('\n'));
  assert.equal(feedText(ATOM), ['Feed: Atom log', 'Item: Entry one', 'Link: https://example.com/e1', 'Published: 2026-10-14T00:00:00Z',
    'Summary: Summary text'].join('\n'));
});

test('malformed, DTD, entity, encoding and oversized feeds are rejected before output', () => {
  const laughs = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><rss><channel><title>&lol2;</title></channel></rss>';
  rejects(laughs, 'source_feed_dtd_forbidden');
  rejects('<!DOCTYPE rss SYSTEM "file:///etc/passwd"><rss><channel/></rss>', 'source_feed_dtd_forbidden');
  rejects('<rss><channel><title>&lol;</title></channel></rss>', 'source_feed_entity_forbidden');
  rejects('<rss><channel><title>&#x0;</title></channel></rss>', 'source_feed_malformed');
  rejects('<rss><channel><title>a & b</title></channel></rss>', 'source_feed_malformed');
  rejects('<rss><channel><title>open</channel></rss>', 'source_feed_malformed');
  rejects('<rss><channel></channel></rss><rss/>', 'source_feed_malformed');
  rejects('<rss a="1" a="2"><channel/></rss>', 'source_feed_malformed');
  rejects('<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel/></rss>', 'source_feed_malformed');
  rejects('<html><body/></html>', 'source_feed_malformed');
  rejects('<rss><channel><![CDATA[x]]></channel><!-- a -- b --></rss>', 'source_feed_malformed');
  rejects('<rss><channel>' + '<item><title>x</title></item>'.repeat(FEED_LIMITS.maxItems + 1) + '</channel></rss>', 'source_output_oversize');
  rejects('<rss>' + '<a>'.repeat(40) + '</a>'.repeat(40) + '</rss>', 'source_output_oversize');
  rejects('<rss><channel><title>' + 'x'.repeat(FEED_LIMITS.maxInputBytes) + '</title></channel></rss>', 'source_output_oversize');
  rejects('<rss><channel><item><description>' + 'word '.repeat(1000) + '</description></item></channel></rss>', 'source_output_oversize');
});

function sourceClient(media_type: string, body: string, calls: string[]): ScopedRequestClient {
  const content = JSON.stringify({ schema_version: 1, source_slot: 0, media_type, body });
  return { isActive: () => true, request: async (method, input) => {
    calls.push(method);
    assert.equal(method, 'artifacts.read');
    return { status: 'succeeded', memory_status: 'no_learning', value: { artifact_ref: (input as {artifact_ref: string}).artifact_ref,
      digest: sha(content), size_bytes: Buffer.byteLength(content), content } };
  } };
}
const extract = { artifact_ref: 'art_raw0', acquisition_receipt_ref: 'discovery-source:goal-discovery:x:0', source_slot: 0, first_line: 1, last_line: 4096 };

test('one source service dispatches by configured media type; replacement needs no consumer edit', async () => {
  const base = profile();
  const composition = new Composition(base); await composition.start();
  const calls: string[] = [];
  try {
    const html = await composition.invoke('source-extraction.extract', extract, sourceClient('text/html', '<p>Hello <b>world</b></p>', calls));
    assert.equal(html.status, 'succeeded');
    if (html.status !== 'succeeded') return;
    assert.deepEqual(html.value.evidence, [{ source_ref: 'source:0:0', text: 'Hello world' }]);
    assert.equal(html.value.provider_digest, composition.states().find(state => state.id === 'seraph.source-extraction.v1')!.digest);
    const feed = await composition.invoke('source-extraction.extract', extract, sourceClient('application/rss+xml', RSS, calls));
    assert.equal(feed.status === 'succeeded' && feed.value.evidence[0]!.text.startsWith('Feed: Release & notes'), true);
    assert.deepEqual(await composition.invoke('source-extraction.extract', extract, sourceClient('application/rss+xml', '<!DOCTYPE x><rss/>', calls)),
      { status: 'blocked', reason_code: 'source_feed_dtd_forbidden', memory_status: 'no_learning' });
    assert.deepEqual(await composition.invoke('source-extraction.extract', extract, sourceClient('application/pdf', '%PDF', calls)),
      { status: 'blocked', reason_code: 'source_format_provider_unavailable', memory_status: 'no_learning' });
  } finally { await composition.dispose(); }
  // Profile-only replacement: map text/html to the plain-text provider and drop feeds.
  const replaced = validateProfile({ ...base, plugins: base.plugins.map(spec => spec.id === 'seraph.source-extraction.v1'
    ? { ...spec, config: { formats: { 'text/html': 'plain-text' } } } : spec) } as never);
  const other = new Composition(replaced); await other.start();
  try {
    const html = await other.invoke('source-extraction.extract', extract, sourceClient('text/html', '<p>Hello <b>world</b></p>', calls));
    assert.equal(html.status === 'succeeded' && html.value.evidence[0]!.text, '<p>Hello <b>world</b></p>');
    assert.notEqual(html.status === 'succeeded' && html.value.config_digest, sha('{}'));
    assert.deepEqual(await other.invoke('source-extraction.extract', extract, sourceClient('application/rss+xml', RSS, calls)),
      { status: 'blocked', reason_code: 'source_format_provider_unavailable', memory_status: 'no_learning' });
  } finally { await other.dispose(); }
  assert.throws(() => validateProfile({ ...base, plugins: base.plugins.map(spec => spec.id === 'seraph.source-extraction.v1'
    ? { ...spec, config: { formats: { 'text/html': 'python-fallback' } } } : spec) } as never), /configuration/);
});
