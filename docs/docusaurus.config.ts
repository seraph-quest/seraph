import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Seraph',
  tagline: 'A proactive guardian intelligence dedicated to elevating its human counterpart',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://docs.seraph.quest',
  baseUrl: '/',

  organizationName: 'seraph-quest',
  projectName: 'seraph',

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/seraph-quest/seraph/tree/main/docs/',
          routeBasePath: '/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Seraph',
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/overview/roadmap',
          label: 'Roadmap',
          position: 'left',
        },
        {
          href: 'https://github.com/seraph-quest/seraph',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentation',
          items: [
            {
              label: 'Getting Started',
              to: '/intro',
            },
            {
              label: 'Vision',
              to: '/overview/vision',
            },
            {
              label: 'Roadmap',
              to: '/overview/roadmap',
            },
          ],
        },
        {
          title: 'Development',
          items: [
            {
              label: 'Phase 1 — Persistent Identity',
              to: '/development/phase-1-persistent-identity',
            },
            {
              label: 'Phase 2 — Capable Executor',
              to: '/development/phase-2-capable-executor',
            },
            {
              label: 'Phase 3 — The Observer',
              to: '/development/phase-3-the-observer',
            },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/seraph-quest/seraph',
            },
          ],
        },
      ],
      copyright: `Copyright ${new Date().getFullYear()} Seraph. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
