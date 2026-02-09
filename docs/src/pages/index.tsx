import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--primary', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">{siteConfig.tagline}</p>
        <div className={styles.buttons}>
          <Link
            className="button button--secondary button--lg"
            to="/docs/intro">
            Get Started
          </Link>
          <Link
            className="button button--secondary button--lg"
            style={{marginLeft: '1rem'}}
            to="/docs/overview/vision">
            Read the Vision
          </Link>
        </div>
      </div>
    </header>
  );
}

function QuickLinks() {
  return (
    <section className={styles.quickLinks}>
      <div className="container">
        <div className="row">
          <div className="col col--4">
            <div className="padding--lg">
              <Heading as="h3">Overview</Heading>
              <p>Understand the vision, philosophy, and roadmap behind Seraph.</p>
              <Link to="/docs/overview/vision">Vision &amp; Philosophy</Link>
              {' | '}
              <Link to="/docs/overview/roadmap">Roadmap</Link>
            </div>
          </div>
          <div className="col col--4">
            <div className="padding--lg">
              <Heading as="h3">Development Phases</Heading>
              <p>Detailed implementation plans for each phase of the project.</p>
              <Link to="/docs/development/phase-1-persistent-identity">Phase 1</Link>
              {' | '}
              <Link to="/docs/development/phase-2-capable-executor">Phase 2</Link>
              {' | '}
              <Link to="/docs/development/phase-3-the-observer">Phase 3</Link>
            </div>
          </div>
          <div className="col col--4">
            <div className="padding--lg">
              <Heading as="h3">Architecture</Heading>
              <p>Technical analysis and comparisons with other approaches.</p>
              <Link to="/docs/architecture/tauri-analysis">Tauri Analysis</Link>
              {' | '}
              <Link to="/docs/architecture/feature-comparison">Feature Comparison</Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="Home"
      description="Seraph — A proactive guardian intelligence with a retro 16-bit RPG interface">
      <HomepageHeader />
      <main>
        <QuickLinks />
      </main>
    </Layout>
  );
}
