export type MessageRole = "user" | "agent" | "step" | "error";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  stepNumber?: number;
  toolUsed?: string;
}

export interface WSMessage {
  type: "message" | "ping";
  message: string;
  session_id: string | null;
}

export interface WSResponse {
  type: "step" | "final" | "error" | "pong";
  content: string;
  session_id: string;
  step: number | null;
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
