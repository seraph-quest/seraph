import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'intro',
    'setup',
    {
      type: 'category',
      label: 'Overview',
      items: ['overview/vision', 'overview/roadmap'],
    },
    {
      type: 'category',
      label: 'Development Phases',
      items: [
        'development/phase-1-persistent-identity',
        'development/phase-2-capable-executor',
        'development/phase-3-the-observer',
        'development/phase-4-the-network',
        'development/phase-5-security',
        'development/screen-daemon-research',
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
      ],
    },
    {
      type: 'category',
      label: 'Architecture',
      items: [
        'architecture/tauri-analysis',
        'architecture/feature-comparison',
      ],
    },
  ],
};

export default sidebars;
