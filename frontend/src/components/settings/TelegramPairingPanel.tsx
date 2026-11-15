import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

interface ConsentState { state: string; expires_at: string | null }
interface TelegramStatus {
  configured: boolean;
  pairing_state: string;
  operator_id: number | null;
  chat_id: number | null;
  token_fingerprint: string | null;
  transit: ConsentState | null;
  transport_mode: string;
  live_transport: boolean;
  last_error: string | null;
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

export function normalizeTelegramStatus(value: unknown): TelegramStatus | null {
  const record = recordOf(value);
  if (!record || typeof record.pairing_state !== "string") return null;
  const transit = recordOf(recordOf(record.consent)?.telegram_transit);
  return {
    configured: record.configured === true,
    pairing_state: record.pairing_state,
    operator_id: typeof record.operator_id === "number" ? record.operator_id : null,
    chat_id: typeof record.chat_id === "number" ? record.chat_id : null,
    token_fingerprint: typeof record.token_fingerprint === "string" ? record.token_fingerprint : null,
    transit: transit && typeof transit.state === "string"
      ? { state: transit.state, expires_at: typeof transit.expires_at === "string" ? transit.expires_at : null }
      : null,
    transport_mode: typeof record.transport_mode === "string" ? record.transport_mode : "unknown",
    live_transport: record.live_transport === true,
    last_error: typeof record.last_error === "string" && record.last_error.length <= 128 ? record.last_error : null,
  };
}

const inputClass = "min-w-0 border border-retro-text/20 bg-retro-bg px-1 py-0.5 text-retro-text disabled:opacity-40";
const buttonClass = "border border-retro-text/20 px-2 py-1 text-retro-text/70 disabled:opacity-40";

/** Minimal operator pairing and transit consent over the existing Telegram API. */
export function TelegramPairingPanel() {
  const mounted = useRef(true);
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [operatorId, setOperatorId] = useState("");
  const [chatId, setChatId] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function call(path: string, init?: RequestInit): Promise<unknown> {
    const response = await apiFetch(API_URL + path, { ...init, signal: AbortSignal.timeout(10_000) });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const code = recordOf(recordOf(payload)?.detail)?.code;
      throw new Error("Telegram request failed (" + response.status + (typeof code === "string" ? " · " + code : "") + ").");
    }
    return payload;
  }

  async function refresh() {
    try {
      const next = normalizeTelegramStatus(await call("/api/telegram/status"));
      if (!next) throw new Error("Telegram status response is invalid.");
      if (mounted.current) setStatus(next);
    } catch (failure) {
      if (mounted.current) setMessage((failure instanceof Error ? failure.message : "Telegram status is unavailable.") + (status ? " Showing last-known status." : ""));
    }
  }

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; };
  }, []);

  async function act(path: string, body: Record<string, unknown> | null, done: string) {
    if (busy) return;
    setBusy(true);
    setMessage(null);
    try {
      await call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      if (!mounted.current) return;
      setToken("");
      setMessage(done);
      await refresh();
    } catch (failure) {
      if (mounted.current) setMessage(failure instanceof Error ? failure.message : "Telegram request failed.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  function pair() {
    const operator = Number(operatorId);
    const chat = Number(chatId);
    if (!Number.isSafeInteger(operator) || operator < 1 || !Number.isSafeInteger(chat) || chat < 1) {
      setMessage("Enter the numeric Telegram user ID and chat ID.");
      return;
    }
    void act("/api/telegram/pair", { operator_id: operator, chat_id: chat, ...(token.trim() ? { bot_token: token } : {}) }, "Paired. Grant transit consent before sending notices.");
  }

  const paired = status?.pairing_state === "active";
  const transitActive = status?.transit?.state === "active";
  return (
    <div className="mt-2 border border-retro-text/10 px-2 py-2 text-[9px]" data-testid="telegram-pairing-panel">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-[10px] text-retro-text">Telegram pairing</div>
        <div className="text-retro-text/60">{status ? status.pairing_state.replace(/_/g, " ") : "status loading"}</div>
      </div>
      <div className="mb-1 text-retro-text/50">
        Pair one operator chat and grant transit consent so task inspectors can send neutral notices. The bot token is write-only and stored in the vault.
        {status && !status.live_transport ? " Live Telegram delivery is not shipped yet; transport mode is " + status.transport_mode.replace(/_/g, " ") + "." : ""}
      </div>
      {status && paired && (
        <div className="mb-1 text-retro-text/70">
          user {status.operator_id ?? "unknown"} · chat {status.chat_id ?? "unknown"} · token {status.token_fingerprint ? "fingerprint " + status.token_fingerprint : "not stored"} · transit consent {status.transit?.state ?? "missing"}{status.transit?.expires_at ? " until " + status.transit.expires_at : ""}
        </div>
      )}
      {status?.last_error && (
        <div className="mb-1 text-retro-text/70" data-testid="telegram-last-error">
          Last Telegram error: {status.last_error.replace(/_/g, " ")}. Review the affected task in Work.
        </div>
      )}
      <div className="grid grid-cols-[120px_minmax(0,1fr)] gap-2">
        <label htmlFor="telegram-operator-id" className="text-retro-text/40">Telegram user ID</label>
        <input id="telegram-operator-id" aria-label="Telegram user ID" inputMode="numeric" value={operatorId} onChange={(event) => setOperatorId(event.target.value)} className={inputClass} />
        <label htmlFor="telegram-chat-id" className="text-retro-text/40">Chat ID</label>
        <input id="telegram-chat-id" aria-label="Telegram chat ID" inputMode="numeric" value={chatId} onChange={(event) => setChatId(event.target.value)} className={inputClass} />
        <label htmlFor="telegram-bot-token" className="text-retro-text/40">Bot token</label>
        <input id="telegram-bot-token" aria-label="Telegram bot token (write-only)" type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} className={inputClass} placeholder={status?.token_fingerprint ? "leave blank to keep existing" : "write-only"} />
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={pair} className={buttonClass}>{paired ? "Re-pair Telegram" : "Pair Telegram"}</button>
        <button type="button" disabled={busy || !paired || transitActive} onClick={() => void act("/api/telegram/consent", { boundary: "telegram_transit" }, "Transit consent granted.")} className={buttonClass}>Grant transit consent</button>
        <button type="button" disabled={busy || !transitActive} onClick={() => void act("/api/telegram/consent/telegram_transit/revoke", null, "Transit consent revoked.")} className={buttonClass}>Revoke transit consent</button>
        <button type="button" disabled={busy || !paired} onClick={() => void act("/api/telegram/revoke", null, "Telegram pairing revoked.")} className={buttonClass}>Unpair</button>
      </div>
      {message && <div role="status" className="mt-1 text-retro-text/70">{message}</div>}
    </div>
  );
}
