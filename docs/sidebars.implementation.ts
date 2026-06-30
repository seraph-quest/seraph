import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  implementationSidebar: [
    {
      type: 'category',
      label: 'Core',
      items: [
        'master-roadmap',
        'STATUS',
        'current-app-guide',
        'screenshot-folder-source',
        'release-2026-06-30',
      ],
    },
    {
      type: 'category',
      label: 'Workstreams',
      items: [
        'trust-boundaries',
        'execution-plane',
        'runtime-reliability',
        'presence-and-reach',
        'guardian-intelligence',
        'embodied-ux',
        'ecosystem-and-leverage',
      ],
    },
    {
      type: 'category',
      label: 'Maintainer Mirrors',
      collapsed: true,
      items: [
        'docs-contract',
        'benchmark-status',
        'superiority-delivery',
        'world-class-strategy-delivery',
        'agent-parity-execution-roadmap',
      ],
    },
  ],
};

export default sidebars;
