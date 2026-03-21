import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  legacySidebar: [
    'intro',
    'setup',
    {
      type: 'category',
      label: 'Overview',
      items: ['overview/status-report', 'overview/roadmap'],
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
        'plan/ecosystem-and-leverage',
      ],
    },
    {
      type: 'category',
      label: 'Archived Phases',
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
      label: 'Integrations',
      items: [
        'integrations/things3-mcp',
      ],
    },
    {
      type: 'category',
      label: 'Extensions',
      items: [
        'extensions/overview',
        'extensions/create-a-capability-pack',
        'extensions/manifest-reference',
        'extensions/contribution-types',
        'extensions/validation-and-doctor',
        'extensions/migration-guide',
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
        'architecture/tauri-analysis',
        'architecture/recursive-delegation-research',
        'development/screen-daemon-research',
      ],
    },
    {
      type: 'category',
      label: 'Archived Directions',
      collapsed: true,
      items: [
        'archive/village-first-vision',
        'archive/village-editor',
        'architecture/competitive-agent-research',
        'architecture/feature-comparison',
        'development/openclaw-feature-parity',
        'plan/embodied-ux',
        'roadmap/sections/section-4-embodiment-life-os',
        'roadmap/seasons/season-4-embodied-life-os',
        'roadmap/batches/s4-b1-avatar-reflection',
        'roadmap/batches/s4-b3-world-motivation',
      ],
    },
  ],
};

export default sidebars;
