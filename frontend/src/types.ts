import type { User } from "@auth0/auth0-spa-js";

export type AppPhase = "boot" | "auth" | "ready";
export type ModelStatus = "available" | "degraded" | "unavailable" | "unknown";
export type MessageRole = "user" | "assistant";
export type VoiceVisualState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";

export interface AppConfig {
  agent_id: string;
  agent_api_base: string;
  auth?: {
    enabled: boolean;
    domain: string;
    client_id: string;
    audience: string;
  };
}

export interface ModelEntry {
  id: string;
  display_name?: string;
  provider?: string;
  status: ModelStatus;
  selectable: boolean;
  available?: boolean;
  default?: boolean;
  reason_code?: string | null;
  detail?: string | null;
}

export interface Conversation {
  id: string;
  model?: string | null;
  metadata?: { title?: string; [key: string]: unknown };
  [key: string]: unknown;
}

export interface ToolActivity {
  id: string;
  name?: string;
  label: string;
  status: "running" | "complete";
}

export interface RequiredAction {
  id: string;
  title?: string;
  description?: string;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  text: string;
  pending?: boolean;
  error?: boolean;
  responseId?: string;
  lastEventId?: string;
  activities?: ToolActivity[];
  requiredAction?: RequiredAction;
}

export interface Notice {
  id: string;
  message: string;
  kind: "warning" | "error";
}

export interface HealthState {
  status: string;
  checks?: Array<{ name?: string; status?: string }>;
  ice_server_count?: number;
}

export interface VoiceConfig {
  ice_servers?: Array<{ urls: string | string[]; username?: string; credential?: string }>;
}

export interface VoiceState {
  connected: boolean;
  ready: boolean;
  muted: boolean;
  visual: VoiceVisualState;
  label: string;
  detail: string;
  micLevel: number;
}

export interface DiagnosticsState {
  agentHealth: string;
  voiceHealth: string;
  lastLatency: string;
  medianLatency: string;
  activities: ToolActivity[];
}

export type AuthUser = User | undefined;
