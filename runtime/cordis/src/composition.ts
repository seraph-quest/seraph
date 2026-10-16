import { Context, FiberState, Service, type Fiber, type Plugin } from 'cordis';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { closed, decodeJson, type Json, ProtocolError } from './protocol.js';
import { Resources } from './resources.js';
import type { ScopedRequestClient } from './contracts/client.js';
import type { Input, Result, ServiceMethod } from './contracts/methods.js';
import { invokeService, SERVICE_KEYS, servicePlugins } from './plugins/index.js';
import { object, type Schema } from './contracts/schema.js';
import { FORMAT_PROVIDERS, SOURCE_MEDIA_TYPES } from './plugins/source_extraction/index.js';

export const CORDIS_VERSION = '4.0.0-rc.10';
export const CORDIS_INTEGRITY = 'sha512-xG90nPNQxR272cC4lR/m5LHevegIJvdddQBlKdEAdGz3n+zgH5lsgkg8o9fc2P3T/f+pO5D7FN1HZvkNBiABnw==';
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
export const BUILD_FILES = [
  'package.json',
  'package-lock.json',
  'profile.json',
  'tsconfig.json',
  'scripts/toolchain.mjs',
  'scripts/build-manifest.mjs',
  'src/bootstrap.ts',
  'src/composition.ts',
  'src/protocol.ts',
  'src/resources.ts',
  'src/contracts/client.ts',
  'src/contracts/methods.ts',
  'src/contracts/schema.ts',
  'src/contracts/authority.ts',
  'src/contracts/goals.ts',
  'src/contracts/tasks.ts',
  'src/contracts/capabilities.ts',
  'src/contracts/inference.ts',
  'src/contracts/memory.ts',
  'src/contracts/artifacts.ts',
  'src/contracts/audit.ts',
  'src/contracts/research.ts',
  'src/contracts/conversation.ts',
  'src/contracts/scheduler.ts',
  'src/contracts/connections.ts',
  'src/contracts/agent_presets.ts',
  'src/contracts/agent_loop.ts',
  'src/contracts/guardian.ts',
  'src/contracts/delegation.ts',
  'src/contracts/board_workers.ts',
  'src/contracts/source_extraction.ts',
  'src/plugins/index.ts',
  'src/plugins/proxy.ts',
  'src/plugins/authority/index.ts',
  'src/plugins/goals/index.ts',
  'src/plugins/tasks/index.ts',
  'src/plugins/capabilities/index.ts',
  'src/plugins/inference/index.ts',
  'src/plugins/memory/index.ts',
  'src/plugins/artifacts/index.ts',
  'src/plugins/audit/index.ts',
  'src/plugins/research/index.ts',
  'src/plugins/conversation/index.ts',
  'src/plugins/scheduler/index.ts',
  'src/plugins/connections/index.ts',
  'src/plugins/agent_presets/index.ts',
  'src/plugins/agent_loop/index.ts',
  'src/plugins/guardian/index.ts',
  'src/plugins/delegation/index.ts',
  'src/plugins/board_workers/index.ts',
  'src/plugins/source_extraction/index.ts',
  'src/plugins/source_extraction/html.ts',
  'src/plugins/source_extraction/html-entities.ts',
  'src/plugins/source_extraction/rss-atom.ts',
  'src/plugins/research/guardian.ts',
  'src/providers/goal-plan.ts',
  'dist/src/bootstrap.js',
  'dist/src/composition.js',
  'dist/src/protocol.js',
  'dist/src/resources.js',
  'dist/src/contracts/client.js',
  'dist/src/contracts/methods.js',
  'dist/src/contracts/schema.js',
  'dist/src/contracts/authority.js',
  'dist/src/contracts/goals.js',
  'dist/src/contracts/tasks.js',
  'dist/src/contracts/capabilities.js',
  'dist/src/contracts/inference.js',
  'dist/src/contracts/memory.js',
  'dist/src/contracts/artifacts.js',
  'dist/src/contracts/audit.js',
  'dist/src/contracts/research.js',
  'dist/src/contracts/conversation.js',
  'dist/src/contracts/scheduler.js',
  'dist/src/contracts/connections.js',
  'dist/src/contracts/agent_presets.js',
  'dist/src/contracts/agent_loop.js',
  'dist/src/contracts/guardian.js',
  'dist/src/contracts/delegation.js',
  'dist/src/contracts/board_workers.js',
  'dist/src/contracts/source_extraction.js',
  'dist/src/plugins/index.js',
  'dist/src/plugins/proxy.js',
  'dist/src/plugins/authority/index.js',
  'dist/src/plugins/goals/index.js',
  'dist/src/plugins/tasks/index.js',
  'dist/src/plugins/capabilities/index.js',
  'dist/src/plugins/inference/index.js',
  'dist/src/plugins/memory/index.js',
  'dist/src/plugins/artifacts/index.js',
  'dist/src/plugins/audit/index.js',
  'dist/src/plugins/research/index.js',
  'dist/src/plugins/conversation/index.js',
  'dist/src/plugins/scheduler/index.js',
  'dist/src/plugins/connections/index.js',
  'dist/src/plugins/agent_presets/index.js',
  'dist/src/plugins/agent_loop/index.js',
  'dist/src/plugins/guardian/index.js',
  'dist/src/plugins/delegation/index.js',
  'dist/src/plugins/board_workers/index.js',
  'dist/src/plugins/source_extraction/index.js',
  'dist/src/plugins/source_extraction/html.js',
  'dist/src/plugins/source_extraction/html-entities.js',
  'dist/src/plugins/source_extraction/rss-atom.js',
  'dist/src/plugins/research/guardian.js',
  'dist/src/providers/goal-plan.js',
];
export const PACKAGE_FILES = [...BUILD_FILES, 'dist/build-manifest.json'];
export interface PluginSpec { id: string; required: boolean; dependencies: string[]; config: Record<string, Json>; }
export interface Profile { protocol: 1; profile_id: string; plugins: PluginSpec[]; }
export interface PluginState { id: string; state: 'ready' | 'blocked' | 'stopped'; reason: string | null; digest: string; }
export const HOST_PLUGIN = 'seraph.host-lifecycle@1.0.0';
interface HostLifecycle { readonly ready: boolean; }
declare module 'cordis' { interface Context { hostLifecycle: HostLifecycle; } }

class HostLifecycleService extends Service implements HostLifecycle {
  readonly ready = true;
  constructor(ctx: Context) { super(ctx, 'hostLifecycle'); }
}
const lifecyclePlugin: Plugin.Object<Record<string, Json>> = {
  name: 'seraph.host-lifecycle@1.0.0',
  provide: ['hostLifecycle'],
  apply(ctx) { new HostLifecycleService(ctx); },
};
const registry: Readonly<Record<string, Plugin.Object<Record<string, Json>>>> = Object.freeze({
  'seraph.host-lifecycle@1.0.0': lifecyclePlugin,
  ...servicePlugins({ isActive: () => false, request: async () => { throw new ProtocolError('native invocation required'); } }),
});

/** Source-format selection: reviewed media types to reviewed format providers. */
const formats: Schema<Record<string, string>> = { parse(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > SOURCE_MEDIA_TYPES.length) throw new ProtocolError('invalid source formats');
  for (const [mediaType, provider] of Object.entries(value)) {
    if (!(SOURCE_MEDIA_TYPES as readonly string[]).includes(mediaType) || typeof provider !== 'string' || !Object.hasOwn(FORMAT_PROVIDERS, provider)) throw new ProtocolError('invalid source formats');
  }
  return value as Record<string, string>;
} };
/** Closed per-plugin configuration schemas; unknown plugins or keys fail closed. */
const CONFIG_SCHEMAS: Readonly<Record<string, Schema<Record<string, unknown>>>> = Object.freeze({
  ...Object.fromEntries(Object.keys(registry).map(id => [id, object({})])),
  'seraph.source-extraction.v1': object({ formats }),
});
const SERVICE_KEY_BY_PLUGIN: Readonly<Record<string, (typeof SERVICE_KEYS)[number]>> = Object.freeze(
  Object.fromEntries(Object.keys(servicePlugins({ isActive: () => false, request: async () => { throw new ProtocolError('native invocation required'); } })).map((id, index) => [id, SERVICE_KEYS[index]!])));
export function validateProfile(value: Json): Profile {
  const profile = closed(value, ['protocol', 'profile_id', 'plugins']);
  if (profile.protocol !== 1 || profile.profile_id !== 'seraph-cordis-bootstrap-v1' || !Array.isArray(profile.plugins) || profile.plugins.length > 64) throw new ProtocolError('invalid reviewed profile');
  const identifiers = Object.keys(registry);
  let cursor = 0;
  for (const value of profile.plugins) {
    const spec = closed(value, ['id', 'required', 'dependencies', 'config']);
    // Entries follow the literal registry order: no duplicates, no unknown IDs.
    const position = typeof spec.id === 'string' ? identifiers.indexOf(spec.id, cursor) : -1;
    if (position < 0 || typeof spec.required !== 'boolean' || !Array.isArray(spec.dependencies) || spec.dependencies.length !== 0) throw new ProtocolError('unknown or invalid plugin');
    cursor = position + 1;
    try { CONFIG_SCHEMAS[spec.id as string]!.parse(spec.config); } catch { throw new ProtocolError('invalid plugin configuration'); }
  }
  // The host plugin is mandatory; service providers may be optional, in which
  // case their methods return typed blocks instead of fencing readiness.
  const host = profile.plugins[0] as Record<string, Json> | undefined;
  if (host?.id !== HOST_PLUGIN || host.required !== true) throw new ProtocolError('required host plugin missing');
  return profile as unknown as Profile;
}
/** Files whose reviewed hashes identify one provider implementation. */
// Provider implementations beyond each domain's own index/contract files.
const EXTRA_PLUGIN_FILES: Readonly<Record<string, readonly string[]>> = Object.freeze({
  'seraph.source-extraction.v1': ['src/plugins/source_extraction/html.ts', 'src/plugins/source_extraction/html-entities.ts', 'src/plugins/source_extraction/rss-atom.ts'],
  'seraph.goal-plan.v1': ['src/providers/goal-plan.ts', 'src/contracts/research.ts'],
  'seraph.guardian-research.v1': ['src/plugins/research/guardian.ts', 'src/contracts/research.ts'],
});
export function pluginFiles(id: string): string[] {
  if (id === HOST_PLUGIN) return ['src/composition.ts', 'dist/src/composition.js'];
  const match = /^seraph\.([a-z-]+)\.v1$/.exec(id);
  if (!match || !Object.hasOwn(registry, id)) throw new ProtocolError('unknown plugin');
  const common = ['src/plugins/proxy.ts', 'dist/src/plugins/proxy.js', 'src/contracts/methods.ts', 'dist/src/contracts/methods.js',
    'src/contracts/schema.ts', 'dist/src/contracts/schema.js'];
  const extra = (EXTRA_PLUGIN_FILES[id] ?? []).flatMap(path => [path, 'dist/' + path.replace(/\.ts$/, '.js')]);
  if (id === 'seraph.goal-plan.v1' || id === 'seraph.guardian-research.v1') return [...extra, ...common];
  const name = match[1]!.replace('-', '_');
  return [`src/plugins/${name}/index.ts`, `dist/src/plugins/${name}/index.js`, `src/contracts/${name}.ts`, `dist/src/contracts/${name}.js`,
    ...common, ...extra];
}
export function pluginDigest(spec: PluginSpec, files: Record<string, Json>): string {
  const hashes: Record<string, Json> = {};
  for (const path of pluginFiles(spec.id)) {
    if (typeof files[path] !== 'string') throw new ProtocolError('plugin file not in reviewed build');
    hashes[path] = files[path]!;
  }
  return createHash('sha256').update(canonical({ id: spec.id, required: spec.required, config: spec.config, files: hashes } as unknown as Json)).digest('hex');
}
export function canonical(value: Json): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key]!)).join(',') + '}';
  return JSON.stringify(value);
}
export function packageIdentity(root = PACKAGE_ROOT): { profile: Profile; composition_digest: string; package_digest: string; plugin_digests: Record<string, string> } {
  const profile = validateProfile(decodeJson(readFileSync(join(root, 'profile.json'))));
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) as { packageManager?: string; dependencies?: Record<string, string> };
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8')) as { lockfileVersion?: number; packages?: Record<string, { version?: string; integrity?: string }> };
  const installed = JSON.parse(readFileSync(join(root, 'node_modules/cordis/package.json'), 'utf8')) as { version?: string };
  if (manifest.packageManager !== 'npm@11.8.0' || manifest.dependencies?.cordis !== CORDIS_VERSION || lock.lockfileVersion !== 3 || lock.packages?.['node_modules/cordis']?.version !== CORDIS_VERSION || lock.packages?.['node_modules/cordis']?.integrity !== CORDIS_INTEGRITY || installed.version !== CORDIS_VERSION) throw new ProtocolError('package pin mismatch');
  if (realpathSync(root) !== root || realpathSync(join(root, 'dist/src/bootstrap.js')) !== join(root, 'dist/src/bootstrap.js')) throw new ProtocolError('unexpected runtime path');
  const build = closed(decodeJson(readFileSync(join(root, 'dist/build-manifest.json'))), ['format', 'npm_version', 'files']);
  const buildFiles = closed(build.files, BUILD_FILES);
  if (build.format !== 1 || build.npm_version !== '11.8.0') throw new ProtocolError('invalid build receipt');
  for (const path of BUILD_FILES) if (buildFiles[path] !== createHash('sha256').update(readFileSync(join(root, path))).digest('hex')) throw new ProtocolError('stale or modified build');
  const hash = createHash('sha256');
  for (const path of PACKAGE_FILES) { hash.update(path); hash.update('\0'); hash.update(readFileSync(join(root, path))); hash.update('\0'); }
  const plugin_digests = Object.fromEntries(profile.plugins.map(spec => [spec.id, pluginDigest(spec, buildFiles)]));
  return { profile, composition_digest: createHash('sha256').update(canonical(profile as unknown as Json)).digest('hex'), package_digest: hash.digest('hex'), plugin_digests };
}

export class Composition {
  readonly context = new Context();
  readonly resources = new Resources();
  private fibers: { spec: PluginSpec; fiber: Fiber }[] = [];
  private disposed = false;
  private cordisErrors = 0;
  private readonly pluginDigests: Record<string, string>;
  constructor(readonly profile: Profile, pluginDigests?: Record<string, string>) {
    // Tests may construct without a build receipt; the child always passes the
    // digests computed from the verified build manifest.
    this.pluginDigests = pluginDigests ?? Object.fromEntries(profile.plugins.map(spec => [spec.id, createHash('sha256').update(canonical({ id: spec.id, unbuilt: true } as unknown as Json)).digest('hex')]));
    this.context.logger.bufferSize = 0;
    // Do not export upstream logs to stdout or expose arbitrary exception text.
    let removeLogger: (() => Promise<void>) | undefined;
    this.resources.track('cordis.logger', async () => { await removeLogger?.(); });
    removeLogger = this.context.logger.exporter({ export: message => { if (message.type === 'error') this.cordisErrors++; } });
  }
  async start(): Promise<void> {
    for (const spec of this.profile.plugins) {
      let fiber: Fiber | undefined;
      this.resources.track(spec.id, async () => { await fiber?.dispose(); });
      fiber = await this.context.plugin(registry[spec.id]!, spec.config);
      this.fibers.push({ spec, fiber });
    }
    this.assertReady();
  }
  assertReady(): void {
    if (this.disposed || this.cordisErrors || this.fibers.some(({ spec, fiber }) => spec.required && (fiber.state !== FiberState.ACTIVE || (SERVICE_KEY_BY_PLUGIN[spec.id] !== undefined && !this.context.get(SERVICE_KEY_BY_PLUGIN[spec.id]!)))) || this.context.get('hostLifecycle')?.ready !== true) throw new ProtocolError('required plugin unavailable');
  }
  private composed(id: string): boolean {
    const entry = this.fibers.find(({ spec }) => spec.id === id);
    return entry !== undefined && entry.fiber.state === FiberState.ACTIVE && !!this.context.get(SERVICE_KEY_BY_PLUGIN[id]!);
  }
  async invoke<M extends ServiceMethod>(method: M, input: Input<M>, client: ScopedRequestClient): Promise<Result<M>> {
    this.assertReady();
    const provider = `seraph.${method.slice(0, method.indexOf('.'))}.v1`;
    if (!this.composed(provider)) return { status: 'blocked', reason_code: 'service_provider_not_composed', memory_status: 'no_learning' } as Result<M>;
    const context = new Context();
    context.logger.bufferSize = 0;
    const listed = new Set(this.fibers.filter(({ spec }) => this.composed(spec.id)).map(({ spec }) => spec.id));
    const plugins = Object.fromEntries(Object.entries(servicePlugins(client, this.pluginDigests)).filter(([id]) => listed.has(id)));
    const configs = Object.fromEntries(this.profile.plugins.map(spec => [spec.id, spec.config]));
    const fibers: Fiber[] = [];
    const id = `invocation-scope-${this.scopeSequence++}`;
    let scopeErrors = 0;
    let removeLogger: (() => Promise<void>) | undefined;
    this.resources.track(id, async () => {
      await Promise.all(fibers.map(fiber => fiber.dispose()));
      await removeLogger?.();
      if (context.registry.size || Object.values(SERVICE_KEY_BY_PLUGIN).some(key => context.get(key)) || scopeErrors) throw new ProtocolError('invocation scope cleanup unknown');
    });
    try {
      removeLogger = context.logger.exporter({ export: message => { if (message.type === 'error') scopeErrors++; } });
      for (const [pluginId, plugin] of Object.entries(plugins)) fibers.push(await context.plugin(plugin as Plugin.Object<Record<string, Json>>, configs[pluginId] ?? {}));
      if (fibers.some(fiber => fiber.state !== FiberState.ACTIVE)) throw new ProtocolError('invocation provider unavailable');
      const result = await invokeService(context, method, input);
      this.assertReady();
      return result;
    } finally { await this.resources.release(id); }
  }
  private scopeSequence = 0;
  states(): PluginState[] {
    return this.fibers.map(({ spec, fiber }) => ({ id: spec.id, state: this.disposed ? 'stopped' : fiber.state === FiberState.ACTIVE && this.context.get('hostLifecycle')?.ready ? 'ready' : 'blocked', reason: this.disposed ? null : fiber.state === FiberState.ACTIVE ? null : 'plugin_unavailable', digest: this.pluginDigests[spec.id]! }));
  }
  async dispose(): Promise<{ resources_remaining: number; cordis_disposal: 'confirmed' | 'unconfirmed' }> {
    this.disposed = true;
    // Fiber disposers are tracked separately and run before removing the logger.
    await this.resources.dispose();
    const absent = this.context.get('hostLifecycle') === undefined && this.context.registry.size === 0;
    return { resources_remaining: this.resources.remaining, cordis_disposal: this.resources.clean && absent && this.cordisErrors === 0 ? 'confirmed' : 'unconfirmed' };
  }
}
