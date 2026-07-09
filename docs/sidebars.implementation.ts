import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  implementationSidebar: [
    {
      type: 'category',
      label: 'Core',
      items: [
        'project-constitution',
        'current-app-guide',
        'STATUS',
        {
          type: 'category',
          label: 'Architecture Decisions',
          collapsed: true,
          items: [
            'decisions/inference-only-model-providers',
            'decisions/one-gpu-serial-priority-scheduling',
            'decisions/canonical-memory-boundary',
            'decisions/gpu-core-mac-edge-topology',
            'decisions/epic-integration-branch-workflow',
          ],
        },
        'docs-contract',
        'screenshot-folder-source',
        'release-2026-07-04',
        'release-2026-06-30',
      ],
    },
    {
      type: 'category',
      label: 'Historical Strategy Summaries',
      collapsed: true,
      items: ['master-roadmap'],
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
        'benchmark-status',
        'superiority-delivery',
        'world-class-strategy-delivery',
      ],
    },
  ],
};

export default sidebars;
