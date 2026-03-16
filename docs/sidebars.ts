import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  legacySidebar: [
    'intro',
    'setup',
    {
      type: 'category',
      label: 'Overview',
      items: ['overview/vision', 'overview/status-report', 'overview/roadmap'],
    },
    {
      type: 'category',
      label: 'Plan',
      items: [
        'plan/trust-boundaries',
        'plan/execution-plane',
        'plan/runtime-reliability',
        'plan/presence-and-reach',
        'plan/guardian-intelligence',
        'plan/embodied-ux',
        'plan/ecosystem-and-leverage',
      ],
    },
    {
      type: 'category',
      label: 'Legacy Phases',
      items: [
        'development/phase-1-persistent-identity',
        'development/phase-2-capable-executor',
        'development/phase-3-the-observer',
        'development/phase-3.5-polish-and-production',
        'development/phase-4-the-network',
        'development/phase-5-security',
      ],
    },
    {
      type: 'category',
      label: 'Tools',
      items: [
        'tools/village-editor',
      ],
    },
    {
      type: 'category',
      label: 'Integrations',
      items: [
        'integrations/things3-mcp',
      ],
    },
    {
      type: 'category',
      label: 'Contributing',
      items: [
        'contributing/git-workflow',
        'development/testing',
      ],
    },
    {
      type: 'category',
      label: 'Architecture',
      items: [
        'architecture/competitive-agent-research',
        'architecture/tauri-analysis',
        'architecture/feature-comparison',
        'architecture/recursive-delegation-research',
        'development/screen-daemon-research',
      ],
    },
  ],
};

export default sidebars;
