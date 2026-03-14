import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'intro',
    'setup',
    {
      type: 'category',
      label: 'Overview',
      items: ['overview/vision', 'overview/roadmap', 'overview/status-report', 'overview/next-steps'],
    },
    {
      type: 'category',
      label: 'Long-Term Plan',
      items: [
        {
          type: 'category',
          label: 'Sections',
          items: [
            'roadmap/sections/section-1-trust-capability',
            'roadmap/sections/section-2-presence-distribution',
            'roadmap/sections/section-3-memory-guardian-intelligence',
            'roadmap/sections/section-4-embodiment-life-os',
            'roadmap/sections/section-5-ecosystem-leverage',
          ],
        },
        {
          type: 'category',
          label: 'Seasons',
          items: [
            'roadmap/seasons/season-1-trust-capability',
            'roadmap/seasons/season-2-reach-presence',
            'roadmap/seasons/season-3-memory-guardian',
            'roadmap/seasons/season-4-embodied-life-os',
          ],
        },
        {
          type: 'category',
          label: 'Batches',
          items: [
            'roadmap/batches/s1-b1-trust-boundaries',
            'roadmap/batches/s1-b2-execution-plane',
            'roadmap/batches/s1-b3-runtime-reliability',
            'roadmap/batches/s2-b1-native-presence',
            'roadmap/batches/s2-b2-channel-reach',
            'roadmap/batches/s2-b3-ambient-guardian',
            'roadmap/batches/s3-b1-human-world-model',
            'roadmap/batches/s3-b2-observer-deepening',
            'roadmap/batches/s3-b3-learning-loop',
            'roadmap/batches/s4-b1-avatar-reflection',
            'roadmap/batches/s4-b2-life-os-surfaces',
            'roadmap/batches/s4-b3-world-motivation',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: 'Development Phases',
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
