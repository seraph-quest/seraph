import { useEffect, useRef, useState } from "react";
import type { AttentionItem, AttentionOwner } from "../lib/cockpitAttention";
import type { GuardianInboxItem } from "../types";

export interface AttentionOrigin {
  ownerKey: string; taskId: string; approvalId?: string | null; origin: "home" | "inbox";
  itemId: string | null; goalId: string | null; threadId: string | null;
}

export function useAttentionNavigation(owner: AttentionOwner | null) {
  const key = owner ? `${owner.principalId}:${owner.sessionId}` : null;
  const keyRef = useRef(key); keyRef.current = key;
  const [storedOrigin, setOrigin] = useState<AttentionOrigin | null>(null);
  const [storedFocus, setFocus] = useState<{ ownerKey: string; section: "home" | "inbox"; itemId: string | null } | null>(null);
  useEffect(() => { setOrigin(null); setFocus(null); }, [key]);
  const origin = storedOrigin?.ownerKey === key ? storedOrigin : null;
  const focus = storedFocus?.ownerKey === key ? storedFocus : null;
  return {
    origin,
    homeFocusId: focus?.section === "home" ? focus.itemId : null,
    inboxFocusId: focus?.section === "inbox" ? focus.itemId : null,
    fromHome(item: AttentionItem) {
      if (!key || !item.taskId) return false;
      setOrigin({ ownerKey: key, taskId: item.taskId, approvalId: item.approvalId, origin: "home", itemId: item.id, goalId: item.goalId, threadId: item.threadId });
      setFocus(null); return true;
    },
    fromInbox(taskId: string, item?: GuardianInboxItem | null) {
      if (!key) return false;
      setOrigin({ ownerKey: key, taskId, origin: "inbox", itemId: item?.id ?? null, goalId: item?.goal_id ?? null, threadId: null });
      setFocus(null); return true;
    },
    focusInbox(itemId: string) { if (key) setFocus({ ownerKey: key, section: "inbox", itemId }); },
    returnContext() {
      if (!origin || keyRef.current !== origin.ownerKey) return null;
      setFocus({ ownerKey: origin.ownerKey, section: origin.origin, itemId: origin.itemId });
      return origin.origin;
    },
    clear() { setOrigin(null); setFocus(null); },
    isCurrent(ownerKey: string) { return keyRef.current === ownerKey; },
  };
}
