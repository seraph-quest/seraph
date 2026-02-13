import { useState, useEffect, useCallback } from "react";
import { API_URL } from "../config/constants";
import { useChatStore } from "../stores/chatStore";
import { EventBus } from "../game/EventBus";
import { DialogFrame } from "./chat/DialogFrame";
import { InterruptionModeToggle } from "./settings/InterruptionModeToggle";

interface SkillInfo {
  name: string;
  description: string;
  requires_tools: string[];
  user_invocable: boolean;
  enabled: boolean;
}

function SkillRow({
  skill,
  onToggle,
}: {
  skill: SkillInfo;
  onToggle: (name: string, enabled: boolean) => void;
}) {
  const statusColor = skill.enabled ? "bg-green-400" : "bg-retro-text/30";

  return (
    <div className="flex items-center gap-1 px-1 py-0.5 border-b border-retro-text/10 last:border-b-0">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${statusColor}`} />
      <div className="flex-1 min-w-0">
        <div className="text-[9px] font-bold text-retro-text truncate">
          {skill.name}
          {skill.user_invocable && (
            <span className="text-retro-highlight/60 ml-1 font-normal">invocable</span>
          )}
        </div>
        <div className="text-[8px] text-retro-text/40 truncate">
          {skill.description}
          {skill.requires_tools.length > 0 && (
            <span> · needs: {skill.requires_tools.join(", ")}</span>
          )}
        </div>
      </div>
      <button
        onClick={() => onToggle(skill.name, !skill.enabled)}
        className={`text-[8px] px-0.5 ${skill.enabled ? "text-green-400 hover:text-red-400" : "text-retro-text/40 hover:text-green-400"}`}
      >
        {skill.enabled ? "on" : "off"}
      </button>
    </div>
  );
}

interface McpServer {
  name: string;
  url: string;
  enabled: boolean;
  connected: boolean;
  tool_count: number;
  description: string;
}

function McpServerRow({
  server,
  onToggle,
  onRemove,
  onTest,
}: {
  server: McpServer;
  onToggle: (name: string, enabled: boolean) => void;
  onRemove: (name: string) => void;
  onTest: (name: string) => void;
}) {
  const statusColor = !server.enabled
    ? "bg-retro-text/30"
    : server.connected
      ? "bg-green-400"
      : "bg-red-400";

  return (
    <div className="flex items-center gap-1 px-1 py-0.5 border-b border-retro-text/10 last:border-b-0">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${statusColor}`} />
      <div className="flex-1 min-w-0">
        <div className="text-[9px] font-bold text-retro-text truncate">{server.name}</div>
        <div className="text-[8px] text-retro-text/40 truncate">
          {server.description || server.url}
          {server.connected && ` · ${server.tool_count} tools`}
        </div>
      </div>
      <button
        onClick={() => onTest(server.name)}
        className="text-[8px] text-retro-text/40 hover:text-retro-highlight px-0.5"
        title="Test connection"
      >
        test
      </button>
      <button
        onClick={() => onToggle(server.name, !server.enabled)}
        className={`text-[8px] px-0.5 ${server.enabled ? "text-green-400 hover:text-red-400" : "text-retro-text/40 hover:text-green-400"}`}
      >
        {server.enabled ? "on" : "off"}
      </button>
      <button
        onClick={() => onRemove(server.name)}
        className="text-[8px] text-retro-text/30 hover:text-red-400 px-0.5"
        title="Remove server"
      >
        x
      </button>
    </div>
  );
}

function AddServerForm({ onAdd }: { onAdd: () => void }) {
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!name.trim() || !url.trim()) {
      setError("Name and URL required");
      return;
    }
    try {
      const res = await fetch(`${API_URL}/api/mcp/servers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          url: url.trim(),
          description: description.trim(),
          enabled: true,
        }),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.detail || "Failed to add server");
        return;
      }
      setName("");
      setUrl("");
      setDescription("");
      setError("");
      setShow(false);
      onAdd();
    } catch {
      setError("Connection failed");
    }
  };

  if (!show) {
    return (
      <button
        onClick={() => setShow(true)}
        className="text-[8px] text-retro-text/40 hover:text-retro-highlight px-1 py-0.5 uppercase tracking-wider"
      >
        + Add server
      </button>
    );
  }

  return (
    <div className="px-1 py-1 border border-retro-text/10 rounded space-y-1">
      <input
        type="text"
        placeholder="Server name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
      />
      <input
        type="text"
        placeholder="URL (e.g. http://host.docker.internal:9100/mcp)"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
      />
      <input
        type="text"
        placeholder="Description (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
      />
      {error && <div className="text-[8px] text-red-400">{error}</div>}
      <div className="flex gap-1">
        <button
          onClick={handleSubmit}
          className="text-[8px] text-retro-highlight hover:text-retro-text uppercase tracking-wider"
        >
          Add
        </button>
        <button
          onClick={() => { setShow(false); setError(""); }}
          className="text-[8px] text-retro-text/40 hover:text-retro-text uppercase tracking-wider"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export function SettingsPanel() {
  const settingsPanelOpen = useChatStore((s) => s.settingsPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const restartOnboarding = useChatStore((s) => s.restartOnboarding);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const debugWalkability = useChatStore((s) => s.debugWalkability);
  const setDebugWalkability = useChatStore((s) => s.setDebugWalkability);

  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [testResult, setTestResult] = useState<string | null>(null);

  const fetchSkills = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/skills`);
      if (res.ok) {
        const data = await res.json();
        setSkills(data.skills ?? []);
      }
    } catch {
      // ignore
    }
  }, []);

  const fetchServers = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/mcp/servers`);
      if (res.ok) {
        const data = await res.json();
        setServers(data.servers ?? []);
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (settingsPanelOpen) {
      fetchSkills();
      fetchServers();
    }
  }, [settingsPanelOpen, fetchSkills, fetchServers]);

  const handleSkillToggle = async (name: string, enabled: boolean) => {
    try {
      await fetch(`${API_URL}/api/skills/${name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      fetchSkills();
    } catch {
      // ignore
    }
  };

  const handleSkillReload = async () => {
    try {
      await fetch(`${API_URL}/api/skills/reload`, { method: "POST" });
      fetchSkills();
    } catch {
      // ignore
    }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    try {
      await fetch(`${API_URL}/api/mcp/servers/${name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      fetchServers();
      useChatStore.getState().fetchToolRegistry();
    } catch {
      // ignore
    }
  };

  const handleRemove = async (name: string) => {
    try {
      await fetch(`${API_URL}/api/mcp/servers/${name}`, { method: "DELETE" });
      fetchServers();
      useChatStore.getState().fetchToolRegistry();
    } catch {
      // ignore
    }
  };

  const handleTest = async (name: string) => {
    setTestResult(`Testing ${name}...`);
    try {
      const res = await fetch(`${API_URL}/api/mcp/servers/${name}/test`, { method: "POST" });
      const data = await res.json();
      if (res.ok) {
        setTestResult(`${name}: OK — ${data.tool_count} tools`);
      } else {
        setTestResult(`${name}: ${data.detail}`);
      }
    } catch {
      setTestResult(`${name}: connection failed`);
    }
    setTimeout(() => setTestResult(null), 5000);
  };

  if (!settingsPanelOpen) return null;

  return (
    <div className="settings-overlay">
      <DialogFrame
        title="Settings"
        className="flex-1 min-h-0 flex flex-col"
        onClose={() => setSettingsPanelOpen(false)}
      >
        <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar flex flex-col gap-2 pb-1">
          <div className="px-1">
            <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold mb-2">
              General
            </div>
            {onboardingCompleted === true && (
              <button
                onClick={async () => {
                  await restartOnboarding();
                  loadSessions();
                  setSettingsPanelOpen(false);
                }}
                className="text-[9px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
              >
                Restart intro
              </button>
            )}
          </div>

          <InterruptionModeToggle />

          <div className="px-1">
            <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold mb-1">
              Skills
            </div>
            {skills.length > 0 ? (
              <div className="border border-retro-text/10 rounded mb-1">
                {skills.map((s) => (
                  <SkillRow
                    key={s.name}
                    skill={s}
                    onToggle={handleSkillToggle}
                  />
                ))}
              </div>
            ) : (
              <div className="text-[8px] text-retro-text/30 mb-1 px-1">No skills loaded</div>
            )}
            <button
              onClick={handleSkillReload}
              className="text-[8px] text-retro-text/40 hover:text-retro-highlight px-1 py-0.5 uppercase tracking-wider"
            >
              Reload skills
            </button>
          </div>

          {import.meta.env.DEV && (
            <div className="px-1">
              <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold mb-1">
                Debug
              </div>
              <button
                onClick={() => {
                  const next = !debugWalkability;
                  setDebugWalkability(next);
                  EventBus.emit("toggle-debug-walkability", next);
                }}
                className="text-[9px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
              >
                {debugWalkability ? "\u2611" : "\u2610"} Show walkability grid
              </button>
            </div>
          )}

          <div className="px-1">
            <div className="text-[9px] uppercase tracking-wider text-retro-border font-bold mb-1">
              MCP Servers
            </div>
            {servers.length > 0 ? (
              <div className="border border-retro-text/10 rounded mb-1">
                {servers.map((s) => (
                  <McpServerRow
                    key={s.name}
                    server={s}
                    onToggle={handleToggle}
                    onRemove={handleRemove}
                    onTest={handleTest}
                  />
                ))}
              </div>
            ) : (
              <div className="text-[8px] text-retro-text/30 mb-1 px-1">No servers configured</div>
            )}
            {testResult && (
              <div className="text-[8px] text-retro-highlight px-1 mb-1">{testResult}</div>
            )}
            <AddServerForm onAdd={() => {
              fetchServers();
              useChatStore.getState().fetchToolRegistry();
            }} />
          </div>

          <div className="flex-1" />
          <div className="text-[8px] text-retro-text/20 px-1 pb-1">
            Seraph v0.1
          </div>
        </div>
      </DialogFrame>
    </div>
  );
}
