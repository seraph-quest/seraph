import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  implementationSidebar: [
    {
      type: 'category',
      label: 'Core',
      items: [
        'master-roadmap',
        'STATUS',
        'docs-contract',
        'benchmark-status',
        'superiority-delivery',
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
  ],
};

export default sidebars;
