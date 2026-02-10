import { TOOL_NAMES } from "../config/constants";

const TOOL_PATTERNS = [
  /ToolCall\(\s*name\s*=\s*['"](\w+)['"]/i,
  /tool_name\s*[:=]\s*['"](\w+)['"]/i,
  /Calling tool:\s*['"]?(\w+)['"]?/i,
  /Using tool:\s*['"]?(\w+)['"]?/i,
  /"tool"\s*:\s*"(\w+)"/i,
];

const KNOWN_TOOLS: Set<string> = new Set(Object.values(TOOL_NAMES));

export function detectToolFromStep(stepContent: string): string | null {
  for (const pattern of TOOL_PATTERNS) {
    const match = stepContent.match(pattern);
    if (match && match[1]) {
      const toolName = match[1].toLowerCase();
      if (KNOWN_TOOLS.has(toolName)) {
        return toolName;
      }
    }
  }

  for (const tool of KNOWN_TOOLS) {
    if (stepContent.toLowerCase().includes(tool)) {
      return tool;
    }
  }

  return null;
}
