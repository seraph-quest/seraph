import './toolchain.mjs';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';

const paths = ['package.json', 'package-lock.json', 'profile.json', 'tsconfig.json', 'scripts/toolchain.mjs', 'scripts/build-manifest.mjs', 'src/bootstrap.ts', 'src/composition.ts', 'src/protocol.ts', 'src/resources.ts', 'dist/src/bootstrap.js', 'dist/src/composition.js', 'dist/src/protocol.js', 'dist/src/resources.js'];
const files = Object.fromEntries(paths.map(path => [path, createHash('sha256').update(readFileSync(path)).digest('hex')]));
writeFileSync('dist/build-manifest.json', JSON.stringify({ format: 1, npm_version: '11.8.0', files }) + '\n');
