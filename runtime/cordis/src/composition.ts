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
  'src/contracts/agent_loop.ts',
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
  'src/plugins/agent_loop/index.ts',
  'src/plugins/source_extraction/index.ts',
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
  'dist/src/contracts/agent_loop.js',
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
  'dist/src/plugins/agent_loop/index.js',
  'dist/src/plugins/source_extraction/index.js',
];
export const PACKAGE_FILES = [...BUILD_FILES, 'dist/build-manifest.json'];
export interface PluginSpec { id: string; required: boolean; dependencies: string[]; config: Record<string, Json>; }
export interface Profile { protocol: 1; profile_id: string; plugins: PluginSpec[]; }
export interface PluginState { id: string; state: 'ready' | 'blocked' | 'stopped'; reason: string | null; }
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

export function validateProfile(value: Json): Profile {
  const profile = closed(value, ['protocol', 'profile_id', 'plugins']);
  if (profile.protocol !== 1 || profile.profile_id !== 'seraph-cordis-bootstrap-v1' || !Array.isArray(profile.plugins) || profile.plugins.length > 64) throw new ProtocolError('invalid reviewed profile');
  const seen = new Set<string>();
  const identifiers = Object.keys(registry);
  for (const [index, value] of profile.plugins.entries()) {
    const spec = closed(value, ['id', 'required', 'dependencies', 'config']);
    if (typeof spec.id !== 'string' || spec.id !== identifiers[index] || seen.has(spec.id) || spec.required !== true || !Array.isArray(spec.dependencies) || spec.dependencies.length !== 0) throw new ProtocolError('unknown or invalid plugin');
    seen.add(spec.id); closed(spec.config, []);
  }
  // Profiles are reviewed closed inventories, never caller-selected plugins.
  if (seen.size !== Object.keys(registry).length) throw new ProtocolError('required host or service plugin missing');
  return profile as unknown as Profile;
}
export function canonical(value: Json): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key]!)).join(',') + '}';
  return JSON.stringify(value);
}
export function packageIdentity(root = PACKAGE_ROOT): { profile: Profile; composition_digest: string; package_digest: string } {
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
  return { profile, composition_digest: createHash('sha256').update(canonical(profile as unknown as Json)).digest('hex'), package_digest: hash.digest('hex') };
}

export class Composition {
  readonly context = new Context();
  readonly resources = new Resources();
  private fibers: { spec: PluginSpec; fiber: Fiber }[] = [];
  private disposed = false;
  private cordisErrors = 0;
  constructor(readonly profile: Profile) {
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
    if (this.disposed || this.cordisErrors || this.fibers.some(({ spec, fiber }) => spec.required && fiber.state !== FiberState.ACTIVE) || this.context.get('hostLifecycle')?.ready !== true || SERVICE_KEYS.some(key => !this.context.get(key))) throw new ProtocolError('required plugin unavailable');
  }
  async invoke<M extends ServiceMethod>(method: M, input: Input<M>, client: ScopedRequestClient): Promise<Result<M>> {
    this.assertReady();
    const context = new Context();
    context.logger.bufferSize = 0;
    const plugins = servicePlugins(client);
    const fibers: Fiber[] = [];
    const id = `invocation-scope-${this.scopeSequence++}`;
    let scopeErrors = 0;
    let removeLogger: (() => Promise<void>) | undefined;
    this.resources.track(id, async () => {
      await Promise.all(fibers.map(fiber => fiber.dispose()));
      await removeLogger?.();
      if (context.registry.size || SERVICE_KEYS.some(key => context.get(key)) || scopeErrors) throw new ProtocolError('invocation scope cleanup unknown');
    });
    try {
      removeLogger = context.logger.exporter({ export: message => { if (message.type === 'error') scopeErrors++; } });
      for (const plugin of Object.values(plugins)) fibers.push(await context.plugin(plugin, {}));
      if (fibers.some(fiber => fiber.state !== FiberState.ACTIVE)) throw new ProtocolError('invocation provider unavailable');
      const result = await invokeService(context, method, input);
      this.assertReady();
      return result;
    } finally { await this.resources.release(id); }
  }
  private scopeSequence = 0;
  states(): PluginState[] {
    return this.fibers.map(({ spec, fiber }) => ({ id: spec.id, state: this.disposed ? 'stopped' : fiber.state === FiberState.ACTIVE && this.context.get('hostLifecycle')?.ready ? 'ready' : 'blocked', reason: this.disposed ? null : fiber.state === FiberState.ACTIVE ? null : 'plugin_unavailable' }));
  }
  async dispose(): Promise<{ resources_remaining: number; cordis_disposal: 'confirmed' | 'unconfirmed' }> {
    this.disposed = true;
    // Fiber disposers are tracked separately and run before removing the logger.
    await this.resources.dispose();
    const absent = this.context.get('hostLifecycle') === undefined && this.context.registry.size === 0;
    return { resources_remaining: this.resources.remaining, cordis_disposal: this.resources.clean && absent && this.cordisErrors === 0 ? 'confirmed' : 'unconfirmed' };
  }
}
