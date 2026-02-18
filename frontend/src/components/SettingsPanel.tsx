import { useState, useEffect, useCallback } from "react";
import { API_URL } from "../config/constants";
import { useChatStore } from "../stores/chatStore";
import { useDragResize } from "../hooks/useDragResize";
import { ResizeHandles } from "./ResizeHandles";
import { EventBus } from "../game/EventBus";
import { DialogFrame } from "./chat/DialogFrame";
import { InterruptionModeToggle } from "./settings/InterruptionModeToggle";
import { DaemonStatus } from "./settings/DaemonStatus";
import { CaptureModeToggle } from "./settings/CaptureModeToggle";

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
        <div className="text-[10px] font-bold text-retro-text truncate">
          {skill.name}
          {skill.user_invocable && (
            <span className="text-retro-highlight/60 ml-1 font-normal">invocable</span>
          )}
        </div>
        <div className="text-[9px] text-retro-text/40 truncate">
          {skill.description}
          {skill.requires_tools.length > 0 && (
            <span> · needs: {skill.requires_tools.join(", ")}</span>
          )}
        </div>
      </div>
      <button
        onClick={() => onToggle(skill.name, !skill.enabled)}
        className={`text-[9px] px-0.5 ${skill.enabled ? "text-green-400 hover:text-red-400" : "text-retro-text/40 hover:text-green-400"}`}
      >
        {skill.enabled ? "on" : "off"}
      </button>
    </div>
  );
}

interface CatalogItem {
  name: string;
  type: "skill" | "mcp_server";
  description: string;
  category: string;
  requires_tools: string[];
  installed: boolean;
  bundled: boolean;
}

function CatalogRow({
  item,
  onInstall,
  installing,
}: {
  item: CatalogItem;
  onInstall: (name: string) => void;
  installing: string | null;
}) {
  const statusColor = item.installed ? "bg-green-400" : "bg-retro-text/30";
  const typeBadge = item.type === "skill" ? "skill" : "mcp";

  return (
    <div className="flex items-center gap-1 px-1 py-0.5 border-b border-retro-text/10 last:border-b-0">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${statusColor}`} />
      <div className="flex-1 min-w-0">
        <div className="text-[10px] font-bold text-retro-text truncate">
          {item.name}
          <span className="text-retro-highlight/50 ml-1 font-normal">{typeBadge}</span>
        </div>
        <div className="text-[9px] text-retro-text/40 truncate">
          {item.description}
          {item.requires_tools.length > 0 && (
            <span> · needs: {item.requires_tools.join(", ")}</span>
          )}
        </div>
      </div>
      {item.installed ? (
        <span className="text-[9px] text-green-400/60 px-0.5">installed</span>
      ) : (
        <button
          onClick={() => onInstall(item.name)}
          disabled={installing === item.name}
          className="text-[9px] text-retro-highlight hover:text-retro-text px-0.5 disabled:text-retro-text/20"
        >
          {installing === item.name ? "..." : "install"}
        </button>
      )}
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
  status: "disconnected" | "connected" | "auth_required" | "error";
  status_message: string | null;
  has_headers: boolean;
  auth_hint: string;
}

function McpServerRow({
  server,
  onToggle,
  onRemove,
  onTest,
  onSetup,
}: {
  server: McpServer;
  onToggle: (name: string, enabled: boolean) => void;
  onRemove: (name: string) => void;
  onTest: (name: string) => void;
  onSetup: (server: McpServer) => void;
}) {
  const statusColor = !server.enabled
    ? "bg-retro-text/30"
    : server.status === "connected"
      ? "bg-green-400"
      : server.status === "auth_required"
        ? "bg-yellow-400 animate-pulse"
        : server.status === "error"
          ? "bg-red-400"
          : "bg-retro-text/30";

  const statusLabel = !server.enabled
    ? null
    : server.status === "connected"
      ? `${server.tool_count} tools`
      : server.status === "auth_required"
        ? "token needed"
        : server.status === "error"
          ? server.status_message || "error"
          : null;

  const showSetup = server.status === "auth_required" || server.has_headers;

  return (
    <div className="flex items-center gap-1 px-1 py-0.5 border-b border-retro-text/10 last:border-b-0">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${statusColor}`} />
      <div className="flex-1 min-w-0">
        <div className="text-[10px] font-bold text-retro-text truncate">{server.name}</div>
        <div className="text-[9px] text-retro-text/40 truncate">
          {server.description || server.url}
          {statusLabel && ` · ${statusLabel}`}
        </div>
      </div>
      {showSetup && (
        <button
          onClick={() => onSetup(server)}
          className="text-[9px] text-yellow-400 hover:text-retro-highlight px-0.5"
          title="Configure auth token"
        >
          {server.status === "auth_required" ? "setup" : "key"}
        </button>
      )}
      <button
        onClick={() => onTest(server.name)}
        className="text-[9px] text-retro-text/40 hover:text-retro-highlight px-0.5"
        title="Test connection"
      >
        test
      </button>
      <button
        onClick={() => onToggle(server.name, !server.enabled)}
        className={`text-[9px] px-0.5 ${!server.enabled ? "text-retro-text/40 hover:text-green-400" : server.status === "connected" ? "text-green-400 hover:text-red-400" : server.status === "auth_required" ? "text-yellow-400 hover:text-red-400" : server.status === "error" ? "text-red-400 hover:text-red-400" : "text-retro-text/40 hover:text-red-400"}`}
      >
        {server.enabled ? "on" : "off"}
      </button>
      <button
        onClick={() => onRemove(server.name)}
        className="text-[9px] text-retro-text/30 hover:text-red-400 px-0.5"
        title="Remove server"
      >
        x
      </button>
    </div>
  );
}

function TokenConfigForm({
  server,
  onClose,
  onSaved,
}: {
  server: McpServer;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSaveAndTest = async () => {
    if (!token.trim()) return;
    setSaving(true);
    setStatus("Saving...");
    try {
      const saveRes = await fetch(`${API_URL}/api/mcp/servers/${server.name}/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token.trim() }),
      });
      if (!saveRes.ok) {
        const data = await saveRes.json();
        setStatus(data.detail || "Failed to save token");
        setSaving(false);
        return;
      }
      setStatus("Testing...");
      const testRes = await fetch(`${API_URL}/api/mcp/servers/${server.name}/test`, { method: "POST" });
      const testData = await testRes.json();
      if (testData.status === "ok") {
        setStatus(`Connected — ${testData.tool_count} tools`);
        setTimeout(() => { onClose(); onSaved(); }, 1500);
      } else {
        setStatus(testData.message || testData.detail || "Connection failed");
      }
    } catch {
      setStatus("Connection failed");
    }
    setSaving(false);
  };

  return (
    <div className="px-1 py-1 border border-retro-text/10 rounded space-y-1 mx-1 mb-1 bg-retro-bg/50">
      {server.auth_hint && (
        <div className="text-[9px] text-retro-highlight/70">
          {server.auth_hint.split(/(https?:\/\/[^\s)]+)/).map((part, i) =>
            part.match(/^https?:\/\//) ? (
              <a key={i} href={part} target="_blank" rel="noopener noreferrer" className="underline hover:text-retro-text">{part}</a>
            ) : (
              <span key={i}>{part}</span>
            )
          )}
        </div>
      )}
      <input
        type="password"
        placeholder="Paste token here"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
      />
      {status && (
        <div className={`text-[9px] ${status.startsWith("Connected") ? "text-green-400" : "text-retro-highlight"}`}>
          {status}
        </div>
      )}
      <div className="flex gap-1">
        <button
          onClick={handleSaveAndTest}
          disabled={saving || !token.trim()}
          className="text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider disabled:text-retro-text/20"
        >
          Save & Test
        </button>
        <button
          onClick={onClose}
          className="text-[9px] text-retro-text/40 hover:text-retro-text uppercase tracking-wider"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function AddServerForm({ onAdd }: { onAdd: () => void }) {
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!name.trim() || !url.trim()) {
      setError("Name and URL required");
      return;
    }
    try {
      const body: Record<string, unknown> = {
        name: name.trim(),
        url: url.trim(),
        description: description.trim(),
        enabled: true,
      };
      if (authToken.trim()) {
        body.headers = { Authorization: `Bearer ${authToken.trim()}` };
      }
      const res = await fetch(`${API_URL}/api/mcp/servers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.detail || "Failed to add server");
        return;
      }
      setName("");
      setUrl("");
      setDescription("");
      setAuthToken("");
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
        className="text-[9px] text-retro-text/40 hover:text-retro-highlight px-1 py-0.5 uppercase tracking-wider"
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
      <input
        type="password"
        placeholder="Auth token (optional)"
        value={authToken}
        onChange={(e) => setAuthToken(e.target.value)}
        className="w-full bg-transparent text-[9px] text-retro-text border-b border-retro-text/20 px-0.5 py-0.5 outline-none focus:border-retro-highlight"
      />
      {error && <div className="text-[9px] text-red-400">{error}</div>}
      <div className="flex gap-1">
        <button
          onClick={handleSubmit}
          className="text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider"
        >
          Add
        </button>
        <button
          onClick={() => { setShow(false); setError(""); }}
          className="text-[9px] text-retro-text/40 hover:text-retro-text uppercase tracking-wider"
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
  const [catalogItems, setCatalogItems] = useState<CatalogItem[]>([]);
  const [installing, setInstalling] = useState<string | null>(null);
  const [configuringServer, setConfiguringServer] = useState<McpServer | null>(null);

  const { panelRef, dragHandleProps, resizeHandleProps, style, bringToFront } = useDragResize("settings", {
    minWidth: 240,
    minHeight: 200,
  });

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

  const fetchCatalog = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/catalog`);
      if (res.ok) {
        const data = await res.json();
        setCatalogItems(data.items ?? []);
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (settingsPanelOpen) {
      fetchSkills();
      fetchServers();
      fetchCatalog();
    }
  }, [settingsPanelOpen, fetchSkills, fetchServers, fetchCatalog]);

  // Poll MCP servers while panel is open to catch external changes (mcp.sh, API)
  useEffect(() => {
    if (!settingsPanelOpen) return;
    const id = setInterval(fetchServers, 5000);
    return () => clearInterval(id);
  }, [settingsPanelOpen, fetchServers]);

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

  const handleInstall = async (name: string) => {
    setInstalling(name);
    try {
      const res = await fetch(`${API_URL}/api/catalog/install/${name}`, { method: "POST" });
      if (res.ok) {
        fetchCatalog();
        fetchSkills();
        fetchServers();
      }
    } catch {
      // ignore
    }
    setInstalling(null);
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
    <div
      ref={panelRef}
      className="settings-overlay"
      style={style}
      onPointerDown={bringToFront}
    >
      <ResizeHandles resizeHandleProps={resizeHandleProps} />
      <DialogFrame
        title="Settings"
        className="flex-1 min-h-0 flex flex-col"
        onClose={() => setSettingsPanelOpen(false)}
        dragHandleProps={dragHandleProps}
      >
        <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar flex flex-col gap-2 pb-1">
          <div className="px-1">
            <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
              General
            </div>
            {onboardingCompleted === true && (
              <button
                onClick={async () => {
                  await restartOnboarding();
                  loadSessions();
                  setSettingsPanelOpen(false);
                }}
                className="text-[10px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
              >
                Restart intro
              </button>
            )}
          </div>

          <InterruptionModeToggle />

          <DaemonStatus />

          <CaptureModeToggle />

          <div className="px-1">
            <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
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
              <div className="text-[9px] text-retro-text/30 mb-1 px-1">No skills loaded</div>
            )}
            <button
              onClick={handleSkillReload}
              className="text-[9px] text-retro-text/40 hover:text-retro-highlight px-1 py-0.5 uppercase tracking-wider"
            >
              Reload skills
            </button>
          </div>

          <div className="px-1">
            <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
              Discover
            </div>
            {catalogItems.length > 0 ? (
              <div className="border border-retro-text/10 rounded mb-1">
                {catalogItems.map((item) => (
                  <CatalogRow
                    key={item.name}
                    item={item}
                    onInstall={handleInstall}
                    installing={installing}
                  />
                ))}
              </div>
            ) : (
              <div className="text-[9px] text-retro-text/30 mb-1 px-1">No catalog items available</div>
            )}
          </div>

          {import.meta.env.DEV && (
            <div className="px-1">
              <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
                Debug
              </div>
              <button
                onClick={() => {
                  const next = !debugWalkability;
                  setDebugWalkability(next);
                  EventBus.emit("toggle-debug-walkability", next);
                }}
                className="text-[10px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
              >
                {debugWalkability ? "\u2611" : "\u2610"} Show walkability grid
              </button>
            </div>
          )}

          <div className="px-1">
            <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
              MCP Servers
            </div>
            {servers.length > 0 ? (
              <div className="border border-retro-text/10 rounded mb-1">
                {servers.map((s) => (
                  <div key={s.name}>
                    <McpServerRow
                      server={s}
                      onToggle={handleToggle}
                      onRemove={handleRemove}
                      onTest={handleTest}
                      onSetup={setConfiguringServer}
                    />
                    {configuringServer?.name === s.name && (
                      <TokenConfigForm
                        server={s}
                        onClose={() => setConfiguringServer(null)}
                        onSaved={() => {
                          fetchServers();
                          useChatStore.getState().fetchToolRegistry();
                        }}
                      />
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-[9px] text-retro-text/30 mb-1 px-1">No servers configured</div>
            )}
            {testResult && (
              <div className="text-[9px] text-retro-highlight px-1 mb-1">{testResult}</div>
            )}
            <AddServerForm onAdd={() => {
              fetchServers();
              useChatStore.getState().fetchToolRegistry();
            }} />
          </div>

          <div className="flex-1" />
          <div className="text-[9px] text-retro-text/20 px-1 pb-1">
            Seraph v0.1
          </div>
        </div>
      </DialogFrame>
    </div>
  );
}
