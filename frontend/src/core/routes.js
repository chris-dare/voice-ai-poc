const CONVERSATION_PATH = /^\/conversations\/(conv_[A-Za-z0-9]+)\/?$/;

export function conversationIdFromPath(pathname) {
  return CONVERSATION_PATH.exec(pathname)?.[1] || null;
}

export function conversationPath(conversationId) {
  return `/conversations/${encodeURIComponent(conversationId)}`;
}

export function safeReturnTo(value) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "/";
  return value;
}
