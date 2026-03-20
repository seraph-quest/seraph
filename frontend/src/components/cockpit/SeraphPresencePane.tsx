import { useMemo } from "react";

import Seraph, { type SeraphState } from "./Seraph";
import {
  deriveSeraphPresenceState,
  type SeraphPresenceSnapshot,
  type SeraphPresenceState,
} from "./seraphPresence";

interface SeraphPresencePaneProps {
  snapshot: SeraphPresenceSnapshot;
}

function toSeraphState(state: SeraphPresenceState): SeraphState {
  switch (state) {
    case "offline":
    case "error":
    case "approval_wait":
    case "tool_use":
    case "thinking":
    case "idle":
      return state;
    case "responding":
    case "proactive":
      return "idle";
  }
}

export function SeraphPresencePane({ snapshot }: SeraphPresencePaneProps) {
  const descriptor = useMemo(() => deriveSeraphPresenceState(snapshot), [snapshot]);

  return (
    <section className="cockpit-panel cockpit-panel--embedded cockpit-presence-panel" aria-label="Seraph presence">
      <Seraph state={toSeraphState(descriptor.state)} />
    </section>
  );
}
