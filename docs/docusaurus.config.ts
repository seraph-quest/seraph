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
          path: './implementation',
          sidebarPath: './sidebars.implementation.ts',
          editUrl: 'https://github.com/seraph-quest/seraph/tree/develop/docs/',
          routeBasePath: '/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],
  plugins: [
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'research',
        path: './research',
        routeBasePath: 'research',
        sidebarPath: './sidebars.research.ts',
        editUrl: 'https://github.com/seraph-quest/seraph/tree/develop/docs/',
      },
    ],
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'legacy',
        path: './docs',
        routeBasePath: 'legacy',
        sidebarPath: './sidebars.ts',
        editUrl: 'https://github.com/seraph-quest/seraph/tree/develop/docs/',
      },
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
          sidebarId: 'implementationSidebar',
          position: 'left',
          label: 'Implementation',
        },
        {
          type: 'doc',
          docId: 'STATUS',
          position: 'left',
          label: 'Status',
        },
        {
          type: 'docSidebar',
          docsPluginId: 'research',
          sidebarId: 'researchSidebar',
          position: 'left',
          label: 'Research',
        },
        {
          type: 'doc',
          docsPluginId: 'legacy',
          docId: 'intro',
          label: 'Archive',
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
              label: 'Master Roadmap',
              to: '/',
            },
            {
              label: 'Development Status',
              to: '/status',
            },
            {
              label: 'Research Synthesis',
              to: '/research',
            },
          ],
        },
        {
          title: 'Reference',
          items: [
            {
              label: 'Archive',
              to: '/legacy',
            },
            {
              label: 'Setup',
              to: '/legacy/setup',
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
