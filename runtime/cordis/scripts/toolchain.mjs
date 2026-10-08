const [major, minor] = process.versions.node.split('.').map(Number);
if (!((major === 22 && minor >= 12) || major === 24)) {
  throw new Error('Cordis requires Node 22.x >=22.12.0 or 24.x');
}
if (!/^npm\/11\.8\.0(?: |$)/.test(process.env.npm_config_user_agent ?? '')) {
  throw new Error('Cordis package commands require exactly npm 11.8.0');
}
