export const REQUIRED_SCOPES = [
  "openid",
  "profile",
  "email",
  "agents:invoke",
  "conversations:write",
  "conversations:read",
  "conversations:delete",
  "responses:read",
  "responses:cancel",
  "actions:approve",
].join(" ");

export const elements = Object.fromEntries(
  [
    "boot-screen", "auth-screen", "auth-message", "login-button", "app", "sidebar",
    "sidebar-scrim", "open-sidebar", "close-sidebar", "new-chat", "header-new-chat",
    "history-list", "profile-button", "profile-menu", "profile-image", "profile-initials",
    "profile-name", "profile-email", "logout-button", "open-diagnostics",
    "desktop-diagnostics", "diagnostics-dialog", "close-diagnostics", "service-status",
    "notice", "conversation", "empty-state", "messages", "scroll-anchor", "composer",
    "prompt-input", "send-button", "voice-button", "voice-panel", "voice-visual",
    "voice-state", "voice-detail", "mute-button", "end-voice", "mic-level", "bot-audio",
    "agent-health", "voice-health", "last-latency", "median-latency", "latency-pill",
    "tool-activity",
  ].map((id) => [id.replaceAll("-", "_"), document.querySelector(`#${id}`)]),
);

export const state = {
  config: null,
  auth: null,
  user: null,
  conversations: [],
  currentConversation: null,
  activeResponseId: null,
  responseAbort: null,
  generating: false,
  health: null,
  voiceClient: null,
  voiceConnected: false,
  voiceReady: false,
  muted: false,
  authenticationRequired: false,
  botBuffer: "",
  voicePartials: new Map(),
  voiceLatencies: [],
};
