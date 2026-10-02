import { useState } from "react";

import type { CockpitSection } from "../../stores/cockpitLayoutStore";

export const COCKPIT_SECTIONS: Array<{ id: CockpitSection; label: string }> = [
  { id: "home", label: "Home" },
  { id: "inbox", label: "Inbox" },
  { id: "work", label: "Work" },
  { id: "goals", label: "Goals" },
  { id: "library", label: "Library" },
  { id: "connections", label: "Connections" },
];

export interface CockpitSectionNavProps {
  activeSection: CockpitSection;
  onSelect: (section: CockpitSection) => void;
}

export function CockpitSectionNav({ activeSection, onSelect }: CockpitSectionNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);
  const primary = COCKPIT_SECTIONS.slice(0, 4);
  const secondary = COCKPIT_SECTIONS.slice(4);

  const select = (section: CockpitSection) => {
    onSelect(section);
    setMoreOpen(false);
  };

  return (
    <nav className="cockpit-section-nav" aria-label="Workspace sections" data-testid="cockpit-section-nav">
      <div className="cockpit-section-nav-desktop">
        {COCKPIT_SECTIONS.map((section) => (
          <button
            key={section.id}
            type="button"
            className={`cockpit-section-nav-button ${activeSection === section.id ? "active" : ""}`}
            data-testid={`cockpit-section-${section.id}`}
            aria-current={activeSection === section.id ? "page" : undefined}
            onClick={() => select(section.id)}
          >
            {section.label}
          </button>
        ))}
      </div>
      <div className="cockpit-section-nav-mobile">
        {primary.map((section) => (
          <button
            key={section.id}
            type="button"
            className={`cockpit-section-nav-button ${activeSection === section.id ? "active" : ""}`}
            data-testid={`cockpit-section-mobile-${section.id}`}
            aria-current={activeSection === section.id ? "page" : undefined}
            onClick={() => select(section.id)}
          >
            {section.label}
          </button>
        ))}
        <div className="cockpit-section-nav-more">
          <button
            type="button"
            className={`cockpit-section-nav-button ${secondary.some((section) => activeSection === section.id) ? "active" : ""}`}
            aria-expanded={moreOpen}
            aria-haspopup="menu"
            onClick={() => setMoreOpen((current) => !current)}
          >
            More
          </button>
          {moreOpen ? (
            <div className="cockpit-section-nav-more-menu" role="menu" aria-label="More workspace sections">
              {secondary.map((section) => (
                <button
                  key={section.id}
                  type="button"
                  role="menuitem"
                  data-testid={`cockpit-section-mobile-${section.id}`}
                  aria-current={activeSection === section.id ? "page" : undefined}
                  onClick={() => select(section.id)}
                >
                  {section.label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </nav>
  );
}
