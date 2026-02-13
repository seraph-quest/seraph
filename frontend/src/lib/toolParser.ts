import { TOOL_NAMES } from "../config/constants";
import { useChatStore } from "../stores/chatStore";

const TOOL_PATTERNS = [
  /Delegating to\s+(\w+):/i,
  /ToolCall\(\s*name\s*=\s*['"](\w+)['"]/i,
  /tool_name\s*[:=]\s*['"](\w+)['"]/i,
  /Calling tool:\s*['"]?(\w+)['"]?/i,
  /Using tool:\s*['"]?(\w+)['"]?/i,
  /"tool"\s*:\s*"(\w+)"/i,
];

// Static fallback set from native tool names
const STATIC_TOOLS: Set<string> = new Set(Object.values(TOOL_NAMES));

function getKnownTools(): Set<string> {
  const registry = useChatStore.getState().toolRegistry;
  if (registry.length > 0) {
    // Dynamic set from API — includes both native and MCP tools
    return new Set(registry.map((t) => t.name));
  }
  return STATIC_TOOLS;
}

export function detectToolFromStep(stepContent: string): string | null {
  const knownTools = getKnownTools();

  for (const pattern of TOOL_PATTERNS) {
    const match = stepContent.match(pattern);
    if (match && match[1]) {
      const toolName = match[1].toLowerCase();
      if (knownTools.has(toolName)) {
        return toolName;
      }
    }
  }

  for (const tool of knownTools) {
    if (stepContent.toLowerCase().includes(tool)) {
      return tool;
    }
  }

  return null;
}
