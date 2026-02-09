export type MessageRole = "user" | "agent" | "step" | "error" | "proactive";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  stepNumber?: number;
  toolUsed?: string;
  urgency?: number;
  interventionType?: string;
}

export interface WSMessage {
  type: "message" | "ping";
  message: string;
  session_id: string | null;
}

export interface WSResponse {
  type: "step" | "final" | "error" | "pong" | "proactive" | "ambient";
  content: string;
  session_id: string;
  step: number | null;
  urgency?: number;
  intervention_type?: string;
  reasoning?: string;
  state?: string;
  tooltip?: string;
}

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error";

export type AgentAnimationState =
  | "idle"
  | "thinking"
  | "walking"
  | "wandering"
  | "at-well"
  | "at-signpost"
  | "at-bench"
  | "at-tower"
  | "at-forge"
  | "at-clock"
  | "at-mailbox"
  | "speaking";

export type FacingDirection = "left" | "right";

export interface AgentVisualState {
  animationState: AgentAnimationState;
  positionX: number; // percentage 0-100
  facing: FacingDirection;
  speechText: string | null;
}

export interface ToolTarget {
  tool: string;
  positionX: number;
  animationState: AgentAnimationState;
}

export type AmbientState = "idle" | "has_insight" | "goal_behind" | "on_track" | "waiting";

export interface SessionInfo {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message: string | null;
  last_message_role: string | null;
}

export interface GoalInfo {
  id: string;
  parent_id: string | null;
  path: string;
  level: string;
  title: string;
  description: string | null;
  status: string;
  domain: string;
  start_date: string | null;
  due_date: string | null;
  sort_order: number;
  children?: GoalInfo[];
  progress?: number;
}

export interface UserProfileInfo {
  id: string;
  name: string;
  onboarding_completed: boolean;
  preferences_json: string | null;
}

export interface DomainProgress {
  domain: string;
  total: number;
  completed: number;
  percentage: number;
}
