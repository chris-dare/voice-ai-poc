import { createAuth0Client } from "@auth0/auth0-spa-js";
import { apiError, apiFetch, apiJson, getAccessToken } from "./api/client.js";
import { promptAction } from "./core/composer.js";
import { contentSegments } from "./core/message-content.js";
import { conversationIdFromPath, conversationPath, safeReturnTo } from "./core/routes.js";
import { elements, REQUIRED_SCOPES, state } from "./core/state.js";
import { recordBrowserTelemetry, recordNavigationInteractive } from "./core/telemetry.js";
import "../styles.css";

async function initialise() {
  const authCallbackStarted = (
    window.location.search.includes("code=") && window.location.search.includes("state=")
  ) ? performance.now() : null;
  bindEvents();
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load the application configuration.");
    state.config = await response.json();
    recordNavigationInteractive();

    if (!state.config.auth?.enabled) {
      showAuthSetupRequired();
      return;
    }

    state.auth = await createAuth0Client({
      domain: state.config.auth.domain,
      clientId: state.config.auth.client_id,
      cacheLocation: "localstorage",
      useRefreshTokens: true,
      useRefreshTokensFallback: true,
      authorizationParams: {
        audience: state.config.auth.audience,
        scope: REQUIRED_SCOPES,
        redirect_uri: window.location.origin,
      },
    });

    if (window.location.search.includes("code=") && window.location.search.includes("state=")) {
      const result = await state.auth.handleRedirectCallback();
      window.history.replaceState(
        {},
        document.title,
        safeReturnTo(result.appState?.returnTo),
      );
    }

    if (!(await state.auth.isAuthenticated())) {
      if (conversationIdFromPath(window.location.pathname)) {
        await login();
        return;
      }
      showAuth();
      return;
    }

    state.user = await state.auth.getUser();
    showApp();
    const [conversationsLoaded] = await Promise.all([loadConversations(), checkHealth()]);
    if (authCallbackStarted !== null && conversationsLoaded) {
      recordBrowserTelemetry("auth_callback_success", performance.now() - authCallbackStarted);
    }
    await syncConversationRoute();
    window.setInterval(checkHealth, 20_000);
  } catch (error) {
    console.error(error);
    showAuthError(error?.message || "Voice AI could not start.");
  }
}

function bindEvents() {
  elements.login_button.addEventListener("click", login);
  elements.logout_button.addEventListener("click", logout);
  elements.new_chat.addEventListener("click", () => void newConversation());
  elements.header_new_chat.addEventListener("click", () => void newConversation());
  elements.open_sidebar.addEventListener("click", () => toggleSidebar(true));
  elements.close_sidebar.addEventListener("click", () => toggleSidebar(false));
  elements.sidebar_scrim.addEventListener("click", () => toggleSidebar(false));
  elements.profile_button.addEventListener("click", toggleProfileMenu);
  elements.composer.addEventListener("submit", submitPrompt);
  elements.prompt_input.addEventListener("input", updateComposer);
  elements.prompt_input.addEventListener("keydown", handleComposerKeydown);
  elements.voice_button.addEventListener("click", startVoice);
  elements.mute_button.addEventListener("click", toggleMute);
  elements.end_voice.addEventListener("click", endVoice);
  elements.open_diagnostics.addEventListener("click", openDiagnostics);
  elements.desktop_diagnostics.addEventListener("click", openDiagnostics);
  elements.close_diagnostics.addEventListener("click", () => elements.diagnostics_dialog.close());
  elements.diagnostics_dialog.addEventListener("click", (event) => {
    if (event.target === elements.diagnostics_dialog) elements.diagnostics_dialog.close();
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.prompt_input.value = button.dataset.prompt;
      resizeComposer();
      updateComposer();
      elements.prompt_input.focus();
    });
  });
  window.addEventListener("popstate", () => void syncConversationRoute());
  window.addEventListener("beforeunload", () => state.voiceClient?.disconnect());
}

function showAuthSetupRequired() {
  elements.boot_screen.classList.add("hidden");
  elements.auth_screen.classList.remove("hidden");
  elements.auth_message.textContent = "Authentication needs one final configuration value: AUTH0_SPA_CLIENT_ID.";
  elements.login_button.textContent = "Auth0 setup required";
  elements.login_button.disabled = true;
}

function showAuthError(message) {
  elements.boot_screen.classList.add("hidden");
  elements.app.classList.add("hidden");
  elements.auth_screen.classList.remove("hidden");
  elements.auth_message.textContent = message;
  elements.login_button.textContent = "Try again";
  elements.login_button.disabled = false;
}

function showAuth() {
  elements.boot_screen.classList.add("hidden");
  elements.app.classList.add("hidden");
  elements.auth_screen.classList.remove("hidden");
}

function showApp() {
  elements.boot_screen.classList.add("hidden");
  elements.auth_screen.classList.add("hidden");
  elements.app.classList.remove("hidden");
  state.authenticationRequired = false;
  const name = state.user?.name || state.user?.nickname || "Account";
  const email = state.user?.email || "Signed in";
  elements.profile_name.textContent = name;
  elements.profile_email.textContent = email;
  elements.profile_initials.textContent = initials(name);
  if (state.user?.picture) {
    elements.profile_image.src = state.user.picture;
    elements.profile_image.classList.remove("hidden");
    elements.profile_initials.classList.add("hidden");
  }
}

async function login() {
  if (!state.auth) {
    window.location.reload();
    return;
  }
  const returnTo = safeReturnTo(
    `${window.location.pathname}${window.location.search}${window.location.hash}`,
  );
  await state.auth.loginWithRedirect({ appState: { returnTo } });
}

async function logout() {
  await state.auth.logout({ logoutParams: { returnTo: window.location.origin } });
}


async function loadConversations() {
  renderHistoryLoading();
  try {
    let cursor = null;
    const conversations = [];
    do {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("after", cursor);
      const page = await apiJson(`/conversations?${query}`);
      conversations.push(...page.data);
      cursor = page.has_more ? page.next_cursor : null;
    } while (cursor && conversations.length < 500);
    state.conversations = conversations;
    renderHistory();
    return true;
  } catch (error) {
    renderHistoryError();
    handleError(error, "Conversation history could not be loaded.");
    return false;
  }
}

function renderHistoryLoading() {
  elements.history_list.replaceChildren();
  for (let index = 0; index < 4; index += 1) {
    const item = document.createElement("div");
    item.className = "history-skeleton";
    elements.history_list.append(item);
  }
}

function renderHistoryError() {
  const item = document.createElement("p");
  item.className = "history-empty";
  item.textContent = "History is temporarily unavailable.";
  elements.history_list.replaceChildren(item);
}

function renderHistory() {
  elements.history_list.replaceChildren();
  if (!state.conversations.length) {
    const item = document.createElement("p");
    item.className = "history-empty";
    item.textContent = "Your conversations will appear here.";
    elements.history_list.append(item);
    return;
  }
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.classList.toggle("active", conversation.id === state.currentConversation?.id);
    button.textContent = conversation.metadata?.title || "New conversation";
    button.title = button.textContent;
    button.addEventListener("click", () => selectConversation(conversation));
    elements.history_list.append(button);
  }
}

async function selectConversation(conversation, { updateLocation = true } = {}) {
  if (state.generating) await cancelResponse();
  state.currentConversation = conversation;
  if (updateLocation) setConversationLocation(conversation.id);
  renderHistory();
  elements.empty_state.classList.add("hidden");
  elements.messages.replaceChildren();
  toggleSidebar(false);
  showConversationLoading();
  try {
    let cursor = null;
    const responses = [];
    do {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("after", cursor);
      const page = await apiJson(`/conversations/${conversation.id}/responses?${query}`);
      responses.push(...page.data);
      cursor = page.has_more ? page.next_cursor : null;
    } while (cursor && responses.length < 1000);
    elements.messages.replaceChildren();
    for (const response of responses) renderStoredResponse(response);
    if (!responses.length) elements.empty_state.classList.remove("hidden");
    scrollToBottom(false);
  } catch (error) {
    elements.messages.replaceChildren();
    handleError(error, "This conversation could not be opened.");
  }
}

async function syncConversationRoute() {
  const conversationId = conversationIdFromPath(window.location.pathname);
  if (!conversationId) {
    if (state.currentConversation) await newConversation({ updateLocation: false });
    return;
  }
  if (state.currentConversation?.id === conversationId) return;
  try {
    let conversation = state.conversations.find((item) => item.id === conversationId);
    if (!conversation) {
      conversation = await apiJson(`/conversations/${conversationId}`);
      state.conversations.unshift(conversation);
    }
    await selectConversation(conversation, { updateLocation: false });
  } catch (error) {
    await newConversation({ updateLocation: false });
    window.history.replaceState({}, document.title, "/");
    handleError(
      error,
      error?.status === 404
        ? "That conversation does not exist or is not available to this account."
        : "That conversation could not be opened.",
    );
  }
}

function setConversationLocation(conversationId) {
  const path = conversationPath(conversationId);
  if (window.location.pathname !== path) {
    window.history.pushState({}, document.title, path);
  }
}

function showConversationLoading() {
  const loading = createAssistantMessage("Loading conversation…");
  loading.classList.add("pending");
}

function renderStoredResponse(response) {
  const input = inputText(response.input);
  if (input) createUserMessage(input);
  const output = outputText(response.output);
  const assistant = createAssistantMessage(output || statusMessage(response));
  assistant.dataset.responseId = response.id;
  for (const item of response.output || []) {
    if (item.type !== "tool_activity") continue;
    const payload = {
      tool_run_id: item.id,
      name: item.name,
      label: item.label,
    };
    addToolChip(assistant, payload);
    if (item.status === "succeeded") completeToolChip(assistant, payload);
  }
  if (response.status === "failed") assistant.querySelector(".message-content").classList.add("message-error");
  if (response.status === "requires_action" && response.required_action) {
    renderRequiredAction(assistant, response.id, response.required_action);
  }
}

async function newConversation({ updateLocation = true } = {}) {
  if (state.generating) await cancelResponse();
  state.currentConversation = null;
  elements.messages.replaceChildren();
  elements.empty_state.classList.remove("hidden");
  renderHistory();
  toggleSidebar(false);
  if (updateLocation && window.location.pathname !== "/") {
    window.history.pushState({}, document.title, "/");
  }
  elements.prompt_input.focus();
}

async function ensureConversation() {
  if (state.currentConversation) return state.currentConversation;
  const conversation = await apiJson("/conversations", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      agent_id: state.config.agent_id,
      metadata: { channel: "web" },
    }),
  });
  state.currentConversation = conversation;
  state.conversations.unshift(conversation);
  setConversationLocation(conversation.id);
  renderHistory();
  return conversation;
}

async function submitPrompt(event) {
  event.preventDefault();
  const action = promptAction(state.generating, elements.prompt_input.value);
  if (action.type === "stop" || action.type === "interrupt") {
    await cancelResponse();
    if (action.type === "stop") return;
  }
  if (action.type === "ignore") return;
  const text = action.text;

  elements.prompt_input.value = "";
  resizeComposer();
  updateComposer();
  elements.empty_state.classList.add("hidden");
  createUserMessage(text);
  const assistant = createAssistantMessage("");
  assistant.classList.add("pending");
  setGenerating(true);
  scrollToBottom();

  try {
    const conversation = await ensureConversation();
    state.responseAbort = new AbortController();
    const response = await apiFetch("/responses", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        conversation_id: conversation.id,
        input: text,
        stream: true,
        background: true,
      }),
      signal: state.responseAbort.signal,
    });
    if (!response.ok) throw await apiError(response);
    state.activeResponseId = response.headers.get("X-Response-Id");
    assistant.dataset.responseId = state.activeResponseId || "";
    await consumeEventStream(response, assistant);
    assistant.classList.remove("pending");
    await loadConversations();
  } catch (error) {
    assistant.classList.remove("pending");
    if (error.name !== "AbortError") {
      const content = assistant.querySelector(".message-content");
      if (!content.textContent) content.textContent = error.message || "I couldn't complete that request.";
      content.classList.add("message-error");
      handleError(error);
    }
  } finally {
    state.activeResponseId = null;
    state.responseAbort = null;
    setGenerating(false);
  }
}

async function consumeEventStream(response, assistant) {
  if (!response.body) throw new Error("The browser could not read the response stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const records = buffer.split(/\r?\n\r?\n/);
    buffer = records.pop() || "";
    for (const record of records) handleAgentEvent(parseSseRecord(record), assistant);
    if (done) break;
  }
  if (buffer.trim()) handleAgentEvent(parseSseRecord(buffer), assistant);
}

function parseSseRecord(record) {
  let type = "message";
  let id = null;
  const data = [];
  for (const line of record.split(/\r?\n/)) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("id:")) id = line.slice(3).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try { return { type, id, data: JSON.parse(data.join("\n")) }; } catch { return null; }
}

function handleAgentEvent(event, assistant) {
  if (!event) return;
  if (event.id) assistant.dataset.lastEventId = event.id;
  const payload = event.data;
  if (event.type === "response.output_text.delta") {
    const content = assistant.querySelector(".message-content");
    content.textContent += payload.delta || "";
    assistant.classList.add("pending");
    scrollToBottom();
  } else if (event.type === "response.tool.started") {
    addToolChip(assistant, payload);
    addToolActivity(payload, false);
  } else if (event.type === "response.tool.completed") {
    completeToolChip(assistant, payload);
    addToolActivity(payload, true);
  } else if (event.type === "response.completed") {
    assistant.classList.remove("pending");
    const content = assistant.querySelector(".message-content");
    if (!content.textContent) content.textContent = outputText(payload.response?.output);
    renderAssistantContent(content, content.textContent);
  } else if (event.type === "response.requires_action") {
    assistant.classList.remove("pending");
    renderRequiredAction(assistant, payload.response_id, payload.required_action);
  } else if (event.type === "response.failed" || event.type === "response.cancelled") {
    assistant.classList.remove("pending");
    const content = assistant.querySelector(".message-content");
    if (!content.textContent) content.textContent = payload.response?.error?.message || "The response stopped before completion.";
    content.classList.add("message-error");
  }
}

function addToolChip(assistant, payload) {
  const activity = assistant.querySelector(".message-activity");
  const chip = document.createElement("div");
  chip.className = "tool-chip";
  chip.dataset.toolRunId = payload.tool_run_id || "";
  const dot = document.createElement("i");
  const label = document.createElement("span");
  label.textContent = payload.label || readableTool(payload.name) || "Working";
  chip.setAttribute("role", "status");
  chip.append(dot, label);
  activity.hidden = false;
  activity.append(chip);
}

function completeToolChip(assistant, payload) {
  const chip = [...assistant.querySelectorAll(".tool-chip")].find((item) => item.dataset.toolRunId === payload.tool_run_id);
  if (chip) {
    chip.classList.add("complete");
    chip.setAttribute("aria-label", `${payload.label || readableTool(payload.name) || "Tool"} completed`);
  }
}

function addToolActivity(payload, complete) {
  elements.tool_activity.querySelector("p")?.remove();
  const item = document.createElement("div");
  item.className = `activity-item${complete ? " complete" : ""}`;
  const dot = document.createElement("i");
  const label = document.createElement("span");
  label.textContent = `${payload.label || readableTool(payload.name || payload.tool) || "Tool"} · ${complete ? "complete" : "running"}`;
  item.append(dot, label);
  elements.tool_activity.prepend(item);
}

function renderRequiredAction(assistant, responseId, action) {
  if (assistant.querySelector(".approval-card")) return;
  const card = document.createElement("div");
  card.className = "approval-card";
  const title = document.createElement("strong");
  title.textContent = action.title || "Approval required";
  const description = document.createElement("p");
  description.textContent = action.description || "Review this action before it continues.";
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  for (const decision of ["approve", "reject"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = decision === "approve" ? "approve" : "";
    button.textContent = decision === "approve" ? "Approve" : "Not now";
    button.addEventListener("click", () => submitAction(assistant, responseId, action.id, decision));
    actions.append(button);
  }
  card.append(title, description, actions);
  assistant.querySelector(".message-content").append(card);
}

async function submitAction(assistant, responseId, actionId, decision) {
  const card = assistant.querySelector(".approval-card");
  card.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  setGenerating(true);
  try {
    await apiJson(`/responses/${responseId}/actions/${actionId}`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ decision }),
    });
    card.remove();
    assistant.classList.add("pending");
    const response = await apiFetch(`/responses/${responseId}/events`, {
      headers: {
        Accept: "text/event-stream",
        ...(assistant.dataset.lastEventId ? { "Last-Event-ID": assistant.dataset.lastEventId } : {}),
      },
    });
    if (!response.ok) throw await apiError(response);
    await consumeEventStream(response, assistant);
    await loadConversations();
  } catch (error) {
    card.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    handleError(error, "The approval could not be submitted.");
  } finally {
    assistant.classList.remove("pending");
    setGenerating(false);
  }
}

async function cancelResponse() {
  state.responseAbort?.abort();
  if (state.activeResponseId) {
    try {
      await apiJson(`/responses/${state.activeResponseId}/cancel`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
    } catch (error) {
      if (error.code !== "response_not_active") handleError(error, "The response could not be stopped.");
    }
  }
  setGenerating(false);
}

function createUserMessage(text) {
  const article = document.createElement("article");
  article.className = "message user";
  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = text;
  article.append(content);
  elements.messages.append(article);
  return article;
}

function createAssistantMessage(text) {
  const article = document.createElement("article");
  article.className = "message assistant";
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  const content = document.createElement("div");
  content.className = "message-content";
  const activity = document.createElement("div");
  activity.className = "message-activity";
  activity.hidden = true;
  const body = document.createElement("div");
  body.className = "message-body";
  renderAssistantContent(content, text);
  body.append(activity, content);
  article.append(avatar, body);
  elements.messages.append(article);
  return article;
}

function renderAssistantContent(container, text) {
  container.replaceChildren();
  for (const segment of contentSegments(text)) {
    if (segment.type === "code") {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (segment.language) code.dataset.language = segment.language;
      code.textContent = segment.text;
      pre.append(code);
      container.append(pre);
    } else {
      container.append(document.createTextNode(segment.text));
    }
  }
}

function setGenerating(generating) {
  state.generating = generating;
  elements.send_button.classList.toggle("generating", generating);
  elements.send_button.setAttribute(
    "aria-label",
    generating && !elements.prompt_input.value.trim()
      ? "Stop response"
      : generating
        ? "Interrupt and send message"
        : "Send message",
  );
  elements.voice_button.disabled = generating || state.voiceConnected;
  updateComposer();
}

function updateComposer() {
  const hasText = Boolean(elements.prompt_input.value.trim());
  elements.send_button.disabled = !state.generating && !hasText;
  elements.send_button.classList.toggle("interrupt-ready", state.generating && hasText);
  if (state.generating) {
    elements.send_button.setAttribute(
      "aria-label",
      hasText ? "Interrupt and send message" : "Stop response",
    );
  }
  resizeComposer();
}

function resizeComposer() {
  elements.prompt_input.style.height = "auto";
  elements.prompt_input.style.height = `${Math.min(elements.prompt_input.scrollHeight, 180)}px`;
}

function handleComposerKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
}

function toggleSidebar(open) {
  elements.app.classList.toggle("sidebar-open", open);
}

function toggleProfileMenu() {
  const open = elements.profile_menu.classList.toggle("hidden") === false;
  elements.profile_button.setAttribute("aria-expanded", String(open));
}

function openDiagnostics() {
  elements.profile_menu.classList.add("hidden");
  if (!elements.diagnostics_dialog.open) elements.diagnostics_dialog.showModal();
}

async function checkHealth() {
  try {
    const response = await fetch("/healthz", { cache: "no-store" });
    const health = await response.json();
    state.health = health;
    const usable = health.status !== "not_ready";
    const fullyReady = health.status === "ready";
    elements.service_status.className = usable ? "ready" : "error";
    elements.service_status.lastChild.textContent = fullyReady
      ? " All systems ready"
      : usable
        ? " Ready on this device"
        : " Voice is unavailable";
    elements.agent_health.textContent = health.checks?.find((item) => item.name?.includes("Agent"))?.status || (usable ? "Ready" : "Unavailable");
    elements.voice_health.textContent = usable ? (fullyReady ? "Ready" : "Ready locally") : "Unavailable";
    elements.voice_button.disabled = !usable || state.generating || state.voiceConnected;
  } catch {
    elements.service_status.className = "error";
    elements.service_status.lastChild.textContent = " Services unavailable";
    elements.agent_health.textContent = "Unavailable";
    elements.voice_health.textContent = "Unavailable";
    elements.voice_button.disabled = true;
  }
}

async function createVoiceClient() {
  const [{ PipecatClient }, { SmallWebRTCTransport }] = await Promise.all([
    import("@pipecat-ai/client-js"),
    import("@pipecat-ai/small-webrtc-transport"),
  ]);
  const iceServers = (state.health?.ice_servers || []).map((server) => ({
    urls: server.urls,
    username: server.username || undefined,
    credential: server.credential || undefined,
  }));
  state.voiceClient = new PipecatClient({
    transport: new SmallWebRTCTransport({ iceServers }),
    enableCam: false,
    enableMic: true,
    callbacks: {
      onConnected: () => {
        state.voiceConnected = true;
        state.voiceReady = false;
        state.voiceClient.enableMic(false);
        setVoiceState("connecting", "Preparing speech models", "This can take a moment on the first call");
      },
      onDisconnected: finishVoice,
      onTrackStarted: playBotTrack,
      onTrackStopped: stopBotTrack,
      onTransportStateChanged: (transportState) => {
        if (transportState === "connecting") setVoiceState("connecting", "Connecting", "Opening a secure audio channel");
      },
      onUserStartedSpeaking: () => setVoiceState("listening", "Listening", "I can hear you"),
      onUserStoppedSpeaking: () => setVoiceState("thinking", "Thinking", "Working on your request"),
      onBotStartedSpeaking: () => {
        state.botBuffer = "";
        setVoiceState("speaking", "Speaking", "You can interrupt at any time");
      },
      onBotStoppedSpeaking: () => {
        if (state.botBuffer) appendVoiceTranscript("assistant", state.botBuffer.trim(), true);
        state.botBuffer = "";
        setVoiceState("listening", "Listening", "Ask a follow-up");
      },
      onUserTranscript: (data) => appendVoiceTranscript("user", data.text, data.final),
      onBotOutput: (data) => {
        if (!data.spoken) return;
        if (data.aggregated_by === "word") {
          state.botBuffer += data.text;
          appendVoiceTranscript("assistant", state.botBuffer.trimStart(), false);
        } else if (!state.botBuffer) {
          appendVoiceTranscript("assistant", data.text, true);
        }
      },
      onLocalAudioLevel: (level) => {
        elements.mic_level.style.transform = `scaleX(${Math.max(.02, Math.min(1, level * 4))})`;
      },
      onServerMessage: handleVoiceServerMessage,
      onMessageError: (error) => showNotice(error?.message || "A voice message failed.", "error"),
      onError: (error) => {
        showNotice(error?.message || "The voice connection failed.", "error");
        setVoiceState("error", "Voice unavailable", "Check microphone permission and try again");
      },
    },
  });
}

async function startVoice() {
  if (state.voiceConnected) return;
  elements.empty_state.classList.add("hidden");
  elements.voice_panel.classList.remove("hidden");
  elements.voice_button.disabled = true;
  setVoiceState("connecting", "Starting voice", "Requesting microphone access");
  try {
    const conversation = await ensureConversation();
    const accessToken = await getAccessToken();
    await createVoiceClient();
    await state.voiceClient.initDevices();
    await state.voiceClient.connect({
      webrtcRequestParams: {
        endpoint: "/api/offer",
        headers: new Headers({
          Authorization: `Bearer ${accessToken}`,
          "X-Conversation-Id": conversation.id,
        }),
      },
    });
  } catch (error) {
    showNotice(error?.message || "Voice could not start. Check microphone access.", "error");
    setVoiceState("error", "Voice unavailable", "Check microphone permission and HTTPS");
    await state.voiceClient?.disconnect().catch(() => {});
  }
}

async function endVoice() {
  await state.voiceClient?.disconnect();
  finishVoice();
}

function finishVoice() {
  state.voiceConnected = false;
  state.voiceReady = false;
  state.muted = false;
  state.voiceClient = null;
  elements.bot_audio.pause();
  elements.bot_audio.srcObject = null;
  elements.voice_panel.classList.add("hidden");
  elements.mute_button.classList.remove("muted");
  elements.mic_level.style.transform = "scaleX(.02)";
  elements.voice_button.disabled = state.health?.status === "not_ready" || state.generating;
  if (!elements.messages.children.length) elements.empty_state.classList.remove("hidden");
  if (state.currentConversation && state.auth) void loadConversations();
}

async function toggleMute() {
  state.muted = !state.muted;
  await state.voiceClient?.enableMic(!state.muted && state.voiceReady);
  elements.mute_button.classList.toggle("muted", state.muted);
  elements.mute_button.setAttribute("aria-label", state.muted ? "Unmute microphone" : "Mute microphone");
  setVoiceState(state.muted ? "idle" : "listening", state.muted ? "Microphone muted" : "Listening", state.muted ? "Tap the microphone to resume" : "Ask a follow-up");
}

function setVoiceState(visualState, label, detail) {
  elements.voice_visual.dataset.state = visualState;
  elements.voice_state.textContent = label;
  elements.voice_detail.textContent = detail;
}

function handleVoiceServerMessage(message) {
  const type = message?.type;
  const data = message?.data || {};
  if (type === "tool-activity") addToolActivity(data, data.status === "completed");
  if (type === "turn-latency") updateVoiceLatency(data);
  if (type === "session-status" && data.state === "warming") {
    state.voiceReady = false;
    state.voiceClient?.enableMic(false);
    setVoiceState("connecting", "Preparing voice", data.detail || "Warming the local speech model");
  }
  if (type === "session-status" && data.state === "ready") {
    state.voiceReady = true;
    if (!state.muted) state.voiceClient?.enableMic(true);
    setVoiceState("listening", "Listening", "Go ahead—I'm ready");
  }
  if (type === "session-error") {
    showNotice(data.message || "The voice session encountered an error.", "error");
    setVoiceState("error", "Voice unavailable", "End the call and try again");
  }
}

function appendVoiceTranscript(role, text, final) {
  if (!text?.trim()) return;
  elements.empty_state.classList.add("hidden");
  const key = `${role}-voice-partial`;
  let article = state.voicePartials.get(key);
  if (!article) {
    article = role === "user" ? createUserMessage(text) : createAssistantMessage(text);
    if (!final) state.voicePartials.set(key, article);
  } else {
    article.querySelector(".message-content").textContent = text;
  }
  if (final) state.voicePartials.delete(key);
  scrollToBottom();
}

async function playBotTrack(track) {
  if (track.kind !== "audio") return;
  const localTrack = state.voiceClient?.tracks().local.audio;
  if (localTrack?.id === track.id) return;
  elements.bot_audio.srcObject = new MediaStream([track]);
  elements.bot_audio.muted = false;
  elements.bot_audio.volume = 1;
  try { await elements.bot_audio.play(); } catch {
    showNotice("Voice playback was blocked. Check that this tab is not muted, then restart voice.", "error");
  }
}

function stopBotTrack(track) {
  const playing = elements.bot_audio.srcObject?.getAudioTracks?.()[0];
  if (playing?.id !== track.id) return;
  elements.bot_audio.pause();
  elements.bot_audio.srcObject = null;
}

function updateVoiceLatency(payload) {
  const seconds = Number(payload.total ?? payload.latency ?? 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return;
  state.voiceLatencies.push(seconds);
  const sorted = [...state.voiceLatencies].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  const median = sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  elements.last_latency.textContent = `${seconds.toFixed(2)}s`;
  elements.median_latency.textContent = `${median.toFixed(2)}s`;
  elements.latency_pill.textContent = `${seconds.toFixed(1)}s last turn`;
}

function inputText(items = []) {
  return items.flatMap((item) => item.content || []).map((content) => content.text || "").join("\n").trim();
}

function outputText(items = []) {
  return items.flatMap((item) => item.content || []).map((content) => content.text || "").join("\n").trim();
}

function statusMessage(response) {
  if (response.status === "failed") return response.error?.message || "This response failed.";
  if (response.status === "cancelled") return "This response was stopped.";
  if (response.status === "queued" || response.status === "in_progress") return "This response is still running.";
  return "";
}

function readableTool(name = "") {
  return name.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function initials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "AI";
}

function scrollToBottom(smooth = true) {
  window.requestAnimationFrame(() => {
    elements.conversation.scrollTo({
      top: elements.conversation.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
  });
}

function showNotice(message, kind = "warning") {
  elements.notice.textContent = message;
  elements.notice.className = `notice ${kind}`;
  window.clearTimeout(showNotice.timeout);
  showNotice.timeout = window.setTimeout(() => elements.notice.classList.add("hidden"), 7000);
}

function handleError(error, fallback) {
  console.error(error);
  if (error?.status === 401 || error?.authenticationRequired) {
    if (state.authenticationRequired) return;
    state.authenticationRequired = true;
    state.responseAbort?.abort();
    state.voiceClient?.disconnect();
    elements.app.classList.add("hidden");
    elements.boot_screen.classList.add("hidden");
    elements.auth_screen.classList.remove("hidden");
    elements.auth_message.textContent = "Your session needs to be renewed. Sign in again to continue this conversation.";
    elements.login_button.textContent = "Sign in again";
    elements.login_button.disabled = false;
    return;
  }
  showNotice(error?.message || fallback || "Something went wrong.", "error");
}

initialise();
