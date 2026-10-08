import './toolchain.mjs';
import { createHash } from 'node:crypto';
import { lstatSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';

const paths = [
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
// Discovery can only reject extras; the trusted hash inventory above is literal.
for (const root of ['src', 'dist/src']) {
  const discovered = [];
  const visit = (directory, depth = 0) => {
    if (depth > 4 || discovered.length > 80) throw new Error('runtime file inventory exceeds reviewed bounds');
    const entries = readdirSync(directory);
    if (entries.length > 80) throw new Error('runtime directory exceeds reviewed bounds');
    for (const name of entries) {
      const path = `${directory}/${name}`;
      const metadata = lstatSync(path);
      if (metadata.isSymbolicLink()) throw new Error('runtime inventory contains a symlink');
      if (metadata.isDirectory()) visit(path, depth + 1);
      else if (metadata.isFile()) discovered.push(path);
      else throw new Error('runtime inventory contains an unsupported file');
    }
  };
  visit(root);
  const expected = paths.filter(path => path.startsWith(`${root}/`));
  if (discovered.length !== expected.length || discovered.some(path => !expected.includes(path))) throw new Error('runtime source/output inventory differs from reviewed closure');
}
const files = Object.fromEntries(paths.map(path => [path, createHash('sha256').update(readFileSync(path)).digest('hex')]));
writeFileSync('dist/build-manifest.json', JSON.stringify({ format: 1, npm_version: '11.8.0', files }) + '\n');
