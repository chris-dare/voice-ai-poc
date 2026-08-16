import { createAuth0Client, type Auth0Client } from "@auth0/auth0-spa-js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { conversationIdFromPath, conversationPath, safeReturnTo } from "../core/routes.js";
import { recordBrowserTelemetry, recordNavigationInteractive } from "../core/telemetry.js";
import { voiceErrorPresentation } from "../core/voice-errors.js";
import { apiError, createApiClient, type ApiClient, type ApiError, REQUIRED_SCOPES } from "../lib/api";
import { inputText, modelStatusLabel, outputText, preferredModel, readableTool, statusMessage } from "../lib/format";
import type {
  AppConfig,
  AppPhase,
  AuthUser,
  ChatMessage,
  Conversation,
  DiagnosticsState,
  HealthState,
  ModelEntry,
  Notice,
  RequiredAction,
  ToolActivity,
  VoiceConfig,
  VoiceState,
  VoiceVisualState,
} from "../types";

type DataRecord = Record<string, any>;
const recordTelemetry = recordBrowserTelemetry as (event: string, durationMs?: number | null) => void;

interface VoiceClientLike {
  initDevices(): Promise<void>;
  connect(options: DataRecord): Promise<void>;
  disconnect(): Promise<void>;
  enableMic(enabled: boolean): Promise<void> | void;
  tracks(): { local: { audio?: MediaStreamTrack } };
}

const initialVoice: VoiceState = {
  connected: false,
  ready: false,
  muted: false,
  visual: "idle",
  label: "Voice",
  detail: "Ready when you are",
  micLevel: 0,
};

const createId = () => crypto.randomUUID();

export function useVoiceAI() {
  const [phase, setPhase] = useState<AppPhase>("boot");
  const [authMessage, setAuthMessage] = useState("Sign in to continue your conversations across text and voice.");
  const [authButtonLabel, setAuthButtonLabel] = useState("Continue");
  const [authDisabled, setAuthDisabled] = useState(false);
  const [user, setUser] = useState<AuthUser>();
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [selectedModel, setSelectedModelState] = useState<ModelEntry | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [currentConversation, setCurrentConversationState] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [voice, setVoice] = useState<VoiceState>(initialVoice);
  const [toolActivity, setToolActivity] = useState<ToolActivity[]>([]);
  const [voiceLatencies, setVoiceLatencies] = useState<number[]>([]);

  const initialisedRef = useRef(false);
  const authRef = useRef<Auth0Client | null>(null);
  const apiRef = useRef<ApiClient | null>(null);
  const configRef = useRef<AppConfig | null>(null);
  const modelsRef = useRef(models);
  const conversationsRef = useRef(conversations);
  const selectedModelRef = useRef(selectedModel);
  const currentConversationRef = useRef(currentConversation);
  const healthRef = useRef(health);
  const voiceRef = useRef(voice);
  const generatingRef = useRef(generating);
  const activeResponseIdRef = useRef<string | null>(null);
  const responseAbortRef = useRef<AbortController | null>(null);
  const voiceClientRef = useRef<VoiceClientLike | null>(null);
  const voicePartialsRef = useRef(new Map<string, string>());
  const botBufferRef = useRef("");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const noticeTimerRef = useRef<number | null>(null);
  const finishVoiceRef = useRef<() => void>(() => undefined);

  useEffect(() => { modelsRef.current = models; }, [models]);
  useEffect(() => { conversationsRef.current = conversations; }, [conversations]);
  useEffect(() => { selectedModelRef.current = selectedModel; }, [selectedModel]);
  useEffect(() => { currentConversationRef.current = currentConversation; }, [currentConversation]);
  useEffect(() => { healthRef.current = health; }, [health]);
  useEffect(() => { voiceRef.current = voice; }, [voice]);
  useEffect(() => { generatingRef.current = generating; }, [generating]);

  const showNotice = useCallback((message: string, kind: Notice["kind"] = "warning") => {
    if (noticeTimerRef.current) window.clearTimeout(noticeTimerRef.current);
    setNotice({ id: createId(), message, kind });
    noticeTimerRef.current = window.setTimeout(() => setNotice(null), 7000);
  }, []);

  const requireAuthentication = useCallback(() => {
    responseAbortRef.current?.abort();
    void voiceClientRef.current?.disconnect();
    setPhase("auth");
    setAuthMessage("Your session needs to be renewed. Sign in again to continue this conversation.");
    setAuthButtonLabel("Sign in again");
    setAuthDisabled(false);
  }, []);

  const handleError = useCallback((error: unknown, fallback?: string) => {
    const apiErrorValue = error as ApiError;
    console.error(error);
    if (apiErrorValue?.status === 401 || apiErrorValue?.authenticationRequired) {
      requireAuthentication();
      return;
    }
    showNotice(apiErrorValue?.message || fallback || "Something went wrong.", "error");
  }, [requireAuthentication, showNotice]);

  const login = useCallback(async () => {
    const auth = authRef.current;
    if (!auth) {
      window.location.reload();
      return;
    }
    const returnTo = safeReturnTo(`${window.location.pathname}${window.location.search}${window.location.hash}`);
    await auth.loginWithRedirect({ appState: { returnTo } });
  }, []);

  const logout = useCallback(async () => {
    await authRef.current?.logout({ logoutParams: { returnTo: window.location.origin } });
  }, []);

  const loadModels = useCallback(async () => {
    const catalog = await apiRef.current!.json<{ data?: ModelEntry[]; default?: string }>("/models");
    const availableModels = catalog.data || [];
    const remembered = window.localStorage.getItem("voice-ai:model");
    const preferred = preferredModel(availableModels, remembered, catalog.default);
    setModels(availableModels);
    setSelectedModelState(preferred);
    modelsRef.current = availableModels;
    selectedModelRef.current = preferred;
  }, []);

  const loadConversations = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError(false);
    try {
      let cursor: string | null = null;
      const loaded: Conversation[] = [];
      do {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("after", cursor);
        const page: { data: Conversation[]; has_more?: boolean; next_cursor?: string } = await apiRef.current!.json(`/conversations?${query}`);
        loaded.push(...page.data);
        cursor = page.has_more ? page.next_cursor || null : null;
      } while (cursor && loaded.length < 500);
      setConversations(loaded);
      return loaded;
    } catch (error) {
      setHistoryError(true);
      handleError(error, "Conversation history could not be loaded.");
      return [];
    } finally {
      setHistoryLoading(false);
    }
  }, [handleError]);

  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch("/health", { cache: "no-store" });
      const nextHealth = await response.json() as HealthState;
      setHealth(nextHealth);
      healthRef.current = nextHealth;
    } catch {
      const unavailable = { status: "not_ready" };
      setHealth(unavailable);
      healthRef.current = unavailable;
    }
  }, []);

  const storedMessages = useCallback((responses: DataRecord[]): ChatMessage[] => {
    const result: ChatMessage[] = [];
    for (const response of responses) {
      const input = inputText(response.input);
      if (input) result.push({ id: `${response.id}:input`, role: "user", text: input });
      const activities = (response.output || [])
        .filter((item: DataRecord) => item.type === "tool_activity")
        .map((item: DataRecord): ToolActivity => ({
          id: item.id || createId(),
          name: item.name,
          label: item.label || readableTool(item.name) || "Working",
          status: item.status === "succeeded" ? "complete" : "running",
        }));
      result.push({
        id: `${response.id}:output`,
        role: "assistant",
        text: outputText(response.output) || statusMessage(response),
        responseId: response.id,
        error: response.status === "failed",
        pending: response.status === "queued" || response.status === "in_progress",
        activities,
        requiredAction: response.status === "requires_action" ? response.required_action : undefined,
      });
    }
    return result;
  }, []);

  const selectModel = useCallback((model: ModelEntry) => {
    if (!model.selectable || generatingRef.current || voiceRef.current.connected) return;
    setSelectedModelState(model);
    selectedModelRef.current = model;
    window.localStorage.setItem("voice-ai:model", model.id);
  }, []);

  const selectConversation = useCallback(async (conversation: Conversation, updateLocation = true) => {
    if (generatingRef.current) await cancelResponseInternal();
    setCurrentConversationState(conversation);
    currentConversationRef.current = conversation;
    const conversationModel = modelsRef.current.find((model) => model.id === conversation.model && model.selectable);
    if (conversationModel) selectModel(conversationModel);
    if (updateLocation) {
      const path = conversationPath(conversation.id);
      if (window.location.pathname !== path) window.history.pushState({}, document.title, path);
    }
    setConversationLoading(true);
    setMessages([]);
    try {
      let cursor: string | null = null;
      const responses: DataRecord[] = [];
      do {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("after", cursor);
        const page: { data: DataRecord[]; has_more?: boolean; next_cursor?: string } = await apiRef.current!.json(`/conversations/${conversation.id}/responses?${query}`);
        responses.push(...page.data);
        cursor = page.has_more ? page.next_cursor || null : null;
      } while (cursor && responses.length < 1000);
      setMessages(storedMessages(responses));
    } catch (error) {
      handleError(error, "This conversation could not be opened.");
    } finally {
      setConversationLoading(false);
    }
  }, [handleError, selectModel, storedMessages]);

  const newConversation = useCallback(async (updateLocation = true) => {
    if (generatingRef.current) await cancelResponseInternal();
    setCurrentConversationState(null);
    currentConversationRef.current = null;
    setMessages([]);
    setConversationLoading(false);
    if (updateLocation && window.location.pathname !== "/") window.history.pushState({}, document.title, "/");
  }, []);

  const syncConversationRoute = useCallback(async (availableConversations: Conversation[]) => {
    const conversationId = conversationIdFromPath(window.location.pathname);
    if (!conversationId) return;
    try {
      let conversation = availableConversations.find((item) => item.id === conversationId);
      if (!conversation) {
        conversation = await apiRef.current!.json<Conversation>(`/conversations/${conversationId}`);
        setConversations((current) => [conversation!, ...current]);
      }
      await selectConversation(conversation, false);
    } catch (error) {
      await newConversation(false);
      window.history.replaceState({}, document.title, "/");
      const status = (error as ApiError)?.status;
      handleError(error, status === 404
        ? "That conversation does not exist or is not available to this account."
        : "That conversation could not be opened.");
    }
  }, [handleError, newConversation, selectConversation]);

  useEffect(() => {
    if (initialisedRef.current) return;
    initialisedRef.current = true;
    let healthTimer: number | undefined;
    const authCallbackStarted = window.location.search.includes("code=") && window.location.search.includes("state=")
      ? performance.now()
      : null;

    void (async () => {
      try {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (!response.ok) throw new Error("Could not load the application configuration.");
        const config = await response.json() as AppConfig;
        configRef.current = config;
        recordNavigationInteractive();
        if (!config.auth?.enabled) {
          setPhase("auth");
          setAuthMessage("Authentication needs one final configuration value: AUTH0_SPA_CLIENT_ID.");
          setAuthButtonLabel("Auth0 setup required");
          setAuthDisabled(true);
          return;
        }

        const auth = await createAuth0Client({
          domain: config.auth.domain,
          clientId: config.auth.client_id,
          cacheLocation: "localstorage",
          useRefreshTokens: true,
          useRefreshTokensFallback: true,
          authorizationParams: {
            audience: config.auth.audience,
            scope: REQUIRED_SCOPES,
            redirect_uri: window.location.origin,
          },
        });
        authRef.current = auth;

        if (window.location.search.includes("code=") && window.location.search.includes("state=")) {
          const result = await auth.handleRedirectCallback();
          window.history.replaceState({}, document.title, safeReturnTo(result.appState?.returnTo));
        }

        if (!(await auth.isAuthenticated())) {
          setPhase("auth");
          if (conversationIdFromPath(window.location.pathname)) {
            const returnTo = safeReturnTo(`${window.location.pathname}${window.location.search}${window.location.hash}`);
            await auth.loginWithRedirect({ appState: { returnTo } });
          }
          return;
        }

        const authenticatedUser = await auth.getUser();
        setUser(authenticatedUser);
        apiRef.current = createApiClient(auth, config);
        setPhase("ready");
        await loadModels();
        const [loadedConversations] = await Promise.all([loadConversations(), checkHealth()]);
        if (authCallbackStarted !== null) recordTelemetry("auth_callback_success", performance.now() - authCallbackStarted);
        await syncConversationRoute(loadedConversations);
        healthTimer = window.setInterval(checkHealth, 20_000);
      } catch (error) {
        console.error(error);
        setPhase("auth");
        setAuthMessage((error as Error)?.message || "Voice AI could not start.");
        setAuthButtonLabel("Try again");
        setAuthDisabled(false);
      }
    })();

    const onPopState = () => void syncConversationRoute(conversationsRef.current);
    window.addEventListener("popstate", onPopState);
    return () => {
      if (healthTimer) window.clearInterval(healthTimer);
      window.removeEventListener("popstate", onPopState);
      if (noticeTimerRef.current) window.clearTimeout(noticeTimerRef.current);
      void voiceClientRef.current?.disconnect();
    };
  // Initialization is intentionally single-run; mutable state is mirrored in refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ensureConversation = useCallback(async (): Promise<Conversation> => {
    if (currentConversationRef.current) return currentConversationRef.current;
    const conversation = await apiRef.current!.json<Conversation>("/conversations", {
      method: "POST",
      headers: { "Idempotency-Key": createId() },
      body: JSON.stringify({
        agent_id: configRef.current!.agent_id,
        model: selectedModelRef.current?.id,
        metadata: { channel: "web" },
      }),
    });
    setCurrentConversationState(conversation);
    currentConversationRef.current = conversation;
    setConversations((current) => [conversation, ...current.filter((item) => item.id !== conversation.id)]);
    const path = conversationPath(conversation.id);
    if (window.location.pathname !== path) window.history.pushState({}, document.title, path);
    return conversation;
  }, []);

  const updateMessage = useCallback((id: string, updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((current) => current.map((message) => message.id === id ? updater(message) : message));
  }, []);

  const addGlobalToolActivity = useCallback((activity: ToolActivity) => {
    setToolActivity((current) => [activity, ...current.filter((item) => item.id !== activity.id)].slice(0, 30));
  }, []);

  const handleAgentEvent = useCallback((event: { type: string; id?: string | null; data: DataRecord }, messageId: string) => {
    const payload = event.data;
    if (event.id) updateMessage(messageId, (message) => ({ ...message, lastEventId: event.id || undefined }));
    if (event.type === "response.output_text.delta") {
      updateMessage(messageId, (message) => ({ ...message, text: message.text + (payload.delta || ""), pending: true }));
      return;
    }
    if (event.type === "response.tool.started" || event.type === "response.tool.completed") {
      const complete = event.type.endsWith("completed");
      const activity: ToolActivity = {
        id: payload.tool_run_id || createId(),
        name: payload.name,
        label: payload.label || readableTool(payload.name || payload.tool) || "Working",
        status: complete ? "complete" : "running",
      };
      updateMessage(messageId, (message) => ({
        ...message,
        activities: [activity, ...(message.activities || []).filter((item) => item.id !== activity.id)],
      }));
      addGlobalToolActivity(activity);
      return;
    }
    if (event.type === "response.completed") {
      updateMessage(messageId, (message) => ({
        ...message,
        pending: false,
        text: message.text || outputText(payload.response?.output),
      }));
      return;
    }
    if (event.type === "response.requires_action") {
      updateMessage(messageId, (message) => ({ ...message, pending: false, requiredAction: payload.required_action }));
      return;
    }
    if (event.type === "response.failed" || event.type === "response.cancelled") {
      const errorCode = payload.response?.error?.code;
      if (typeof errorCode === "string" && errorCode.startsWith("model_")) {
        void loadModels().catch(() => undefined);
      }
      updateMessage(messageId, (message) => ({
        ...message,
        pending: false,
        error: true,
        text: message.text || payload.response?.error?.message || "The response stopped before completion.",
      }));
    }
  }, [addGlobalToolActivity, loadModels, updateMessage]);

  const consumeEventStream = useCallback(async (response: Response, messageId: string) => {
    if (!response.body) throw new Error("The browser could not read the response stream.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const records = buffer.split(/\r?\n\r?\n/);
      buffer = records.pop() || "";
      for (const record of records) {
        const event = parseSseRecord(record);
        if (event) handleAgentEvent(event, messageId);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const event = parseSseRecord(buffer);
      if (event) handleAgentEvent(event, messageId);
    }
  }, [handleAgentEvent]);

  async function cancelResponseInternal() {
    responseAbortRef.current?.abort();
    const responseId = activeResponseIdRef.current;
    if (responseId) {
      try {
        await apiRef.current!.json(`/responses/${responseId}/cancel`, {
          method: "POST",
          headers: { "Idempotency-Key": createId() },
        });
      } catch (error) {
        if ((error as ApiError).code !== "response_not_active") handleError(error, "The response could not be stopped.");
      }
    }
    activeResponseIdRef.current = null;
    responseAbortRef.current = null;
    setGenerating(false);
    generatingRef.current = false;
  }

  const cancelResponse = useCallback(cancelResponseInternal, [handleError]);

  const sendPrompt = useCallback(async (rawText: string) => {
    const text = rawText.trim();
    if (generatingRef.current) {
      await cancelResponseInternal();
      if (!text) return;
    }
    if (!text || !selectedModelRef.current) return;
    const userMessage: ChatMessage = { id: createId(), role: "user", text };
    const assistantId = createId();
    const assistantMessage: ChatMessage = { id: assistantId, role: "assistant", text: "", pending: true, activities: [] };
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setGenerating(true);
    generatingRef.current = true;
    try {
      const conversation = await ensureConversation();
      const abortController = new AbortController();
      responseAbortRef.current = abortController;
      const response = await apiRef.current!.fetch("/responses", {
        method: "POST",
        headers: { Accept: "text/event-stream", "Idempotency-Key": createId() },
        body: JSON.stringify({
          conversation_id: conversation.id,
          model: selectedModelRef.current.id,
          input: text,
          stream: true,
          background: true,
        }),
        signal: abortController.signal,
      });
      if (!response.ok) throw await apiError(response);
      const responseId = response.headers.get("X-Response-Id");
      activeResponseIdRef.current = responseId;
      updateMessage(assistantId, (message) => ({ ...message, responseId: responseId || undefined }));
      await consumeEventStream(response, assistantId);
      updateMessage(assistantId, (message) => ({ ...message, pending: false }));
      await loadConversations();
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        if (typeof (error as ApiError).code === "string" && (error as ApiError).code!.startsWith("model_")) {
          await loadModels().catch(() => undefined);
        }
        updateMessage(assistantId, (message) => ({
          ...message,
          pending: false,
          error: true,
          text: message.text || (error as Error).message || "I couldn't complete that request.",
        }));
        handleError(error);
      }
    } finally {
      activeResponseIdRef.current = null;
      responseAbortRef.current = null;
      setGenerating(false);
      generatingRef.current = false;
    }
  }, [consumeEventStream, ensureConversation, handleError, loadConversations, updateMessage]);

  const submitAction = useCallback(async (messageId: string, responseId: string, action: RequiredAction, decision: "approve" | "reject") => {
    setGenerating(true);
    generatingRef.current = true;
    updateMessage(messageId, (message) => ({ ...message, pending: true, requiredAction: undefined }));
    try {
      await apiRef.current!.json(`/responses/${responseId}/actions/${action.id}`, {
        method: "POST",
        headers: { "Idempotency-Key": createId() },
        body: JSON.stringify({ decision }),
      });
      const message = messages.find((item) => item.id === messageId);
      const response = await apiRef.current!.fetch(`/responses/${responseId}/events`, {
        headers: {
          Accept: "text/event-stream",
          ...(message?.lastEventId ? { "Last-Event-ID": message.lastEventId } : {}),
        },
      });
      if (!response.ok) throw await apiError(response);
      await consumeEventStream(response, messageId);
      await loadConversations();
    } catch (error) {
      updateMessage(messageId, (message) => ({ ...message, pending: false, requiredAction: action }));
      handleError(error, "The approval could not be submitted.");
    } finally {
      updateMessage(messageId, (message) => ({ ...message, pending: false }));
      setGenerating(false);
      generatingRef.current = false;
    }
  }, [consumeEventStream, handleError, loadConversations, messages, updateMessage]);

  const appendVoiceTranscript = useCallback((role: "user" | "assistant", text: string, final: boolean) => {
    if (!text?.trim()) return;
    const key = `${role}-voice-partial`;
    const existingId = voicePartialsRef.current.get(key);
    if (existingId) {
      updateMessage(existingId, (message) => ({ ...message, text, pending: !final }));
      if (final) voicePartialsRef.current.delete(key);
      return;
    }
    const id = createId();
    setMessages((current) => [...current, { id, role, text, pending: !final && role === "assistant" }]);
    if (!final) voicePartialsRef.current.set(key, id);
  }, [updateMessage]);

  const setVoicePresentation = useCallback((visual: VoiceVisualState, label: string, detail: string) => {
    setVoice((current) => ({ ...current, visual, label, detail }));
  }, []);

  const finishVoice = useCallback(() => {
    voiceClientRef.current = null;
    voicePartialsRef.current.clear();
    botBufferRef.current = "";
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.srcObject = null;
    }
    setVoice(initialVoice);
    if (currentConversationRef.current && authRef.current) void loadConversations();
  }, [loadConversations]);
  finishVoiceRef.current = finishVoice;

  const addVoiceLatency = useCallback((payload: DataRecord) => {
    const seconds = Number(payload.total ?? payload.latency ?? 0);
    if (Number.isFinite(seconds) && seconds > 0) setVoiceLatencies((current) => [...current, seconds]);
  }, []);

  const handleVoiceServerMessage = useCallback((message: DataRecord) => {
    const type = message?.type;
    const data = message?.data || {};
    if (type === "tool-activity") {
      addGlobalToolActivity({
        id: data.tool_run_id || createId(),
        name: data.name || data.tool,
        label: data.label || readableTool(data.name || data.tool) || "Working",
        status: data.status === "completed" ? "complete" : "running",
      });
    }
    if (type === "turn-latency") addVoiceLatency(data);
    if (type === "session-status" && data.state === "warming") {
      setVoice((current) => ({ ...current, ready: false, visual: "connecting", label: "Preparing voice", detail: data.detail || "Warming the local speech model" }));
      void voiceClientRef.current?.enableMic(false);
    }
    if (type === "session-status" && data.state === "ready") {
      setVoice((current) => ({ ...current, ready: true, visual: "listening", label: "Listening", detail: "Go ahead—I'm ready" }));
      if (!voiceRef.current.muted) void voiceClientRef.current?.enableMic(true);
    }
    if (type === "session-error") {
      showNotice(data.message || "The voice session encountered an error.", "error");
      setVoicePresentation("error", "Voice unavailable", "End the call and try again");
    }
  }, [addGlobalToolActivity, addVoiceLatency, setVoicePresentation, showNotice]);

  const createVoiceClient = useCallback(async () => {
    const accessToken = await apiRef.current!.accessToken();
    const voiceConfigResponse = await fetch("/api/voice-config", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!voiceConfigResponse.ok) throw await apiError(voiceConfigResponse);
    const voiceConfig = await voiceConfigResponse.json() as VoiceConfig;
    const [{ PipecatClient }, { SmallWebRTCTransport }] = await Promise.all([
      import("@pipecat-ai/client-js"),
      import("@pipecat-ai/small-webrtc-transport"),
    ]);
    const iceServers = (voiceConfig.ice_servers || []).map((server) => ({
      urls: server.urls,
      username: server.username || undefined,
      credential: server.credential || undefined,
    }));
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport({ iceServers }),
      enableCam: false,
      enableMic: true,
      callbacks: {
        onConnected: () => {
          setVoice((current) => ({ ...current, connected: true, ready: false, visual: "connecting", label: "Preparing voice", detail: "This can take a moment on the first call" }));
          void voiceClientRef.current?.enableMic(false);
        },
        onDisconnected: () => finishVoiceRef.current(),
        onTrackStarted: (track: MediaStreamTrack) => {
          if (track.kind !== "audio") return;
          const localTrack = voiceClientRef.current?.tracks().local.audio;
          if (localTrack?.id === track.id || !audioRef.current) return;
          audioRef.current.srcObject = new MediaStream([track]);
          audioRef.current.muted = false;
          audioRef.current.volume = 1;
          void audioRef.current.play().catch(() => showNotice("Voice playback was blocked. Check that this tab is not muted, then restart voice.", "error"));
        },
        onTrackStopped: (track: MediaStreamTrack) => {
          const playing = audioRef.current?.srcObject instanceof MediaStream ? audioRef.current.srcObject.getAudioTracks()[0] : undefined;
          if (playing?.id !== track.id || !audioRef.current) return;
          audioRef.current.pause();
          audioRef.current.srcObject = null;
        },
        onTransportStateChanged: (transportState: string) => {
          if (transportState === "connecting") setVoicePresentation("connecting", "Connecting", "Opening a secure audio channel");
        },
        onUserStartedSpeaking: () => setVoicePresentation("listening", "Listening", "I can hear you"),
        onUserStoppedSpeaking: () => setVoicePresentation("thinking", "Thinking", "Working on your request"),
        onBotStartedSpeaking: () => {
          botBufferRef.current = "";
          setVoicePresentation("speaking", "Speaking", "You can interrupt at any time");
        },
        onBotStoppedSpeaking: () => {
          if (botBufferRef.current) appendVoiceTranscript("assistant", botBufferRef.current.trim(), true);
          botBufferRef.current = "";
          setVoicePresentation("listening", "Listening", "Ask a follow-up");
        },
        onUserTranscript: (data: DataRecord) => appendVoiceTranscript("user", data.text, data.final),
        onBotOutput: (data: DataRecord) => {
          if (!data.spoken) return;
          if (data.aggregated_by === "word") {
            botBufferRef.current += data.text;
            appendVoiceTranscript("assistant", botBufferRef.current.trimStart(), false);
          } else if (!botBufferRef.current) appendVoiceTranscript("assistant", data.text, true);
        },
        onLocalAudioLevel: (level: number) => setVoice((current) => ({ ...current, micLevel: Math.max(.02, Math.min(1, level * 4)) })),
        onServerMessage: handleVoiceServerMessage,
        onMessageError: (error: DataRecord) => showNotice(String(error?.message || "A voice message failed."), "error"),
        onError: (error: DataRecord) => {
          showNotice(String(error?.message || "The voice connection failed."), "error");
          const presentation = voiceErrorPresentation(error, voiceRef.current.connected);
          setVoice((current) => ({ ...current, visual: presentation.state as VoiceVisualState, label: presentation.label, detail: presentation.detail, ready: Boolean(presentation.recoverable) }));
          if (presentation.recoverable && !voiceRef.current.muted) void voiceClientRef.current?.enableMic(true);
        },
      },
    }) as unknown as VoiceClientLike;
    voiceClientRef.current = client;
    return client;
  }, [appendVoiceTranscript, handleVoiceServerMessage, setVoicePresentation, showNotice]);

  const startVoice = useCallback(async () => {
    if (voiceRef.current.connected || generatingRef.current || !selectedModelRef.current) return;
    setVoice({ ...initialVoice, visual: "connecting", label: "Starting voice", detail: "Requesting microphone access" });
    try {
      const conversation = await ensureConversation();
      const accessToken = await apiRef.current!.accessToken();
      const client = await createVoiceClient();
      await client.initDevices();
      await client.connect({
        webrtcRequestParams: {
          endpoint: "/api/offer",
          headers: new Headers({
            Authorization: `Bearer ${accessToken}`,
            "X-Conversation-Id": conversation.id,
            "X-Model-Id": selectedModelRef.current?.id || "",
          }),
        },
      });
    } catch (error) {
      showNotice((error as Error)?.message || "Voice could not start. Check microphone access.", "error");
      setVoice({ ...initialVoice, visual: "error", label: "Voice unavailable", detail: "Check microphone permission and HTTPS" });
      await voiceClientRef.current?.disconnect().catch(() => undefined);
    }
  }, [createVoiceClient, ensureConversation, showNotice]);

  const endVoice = useCallback(async () => {
    await voiceClientRef.current?.disconnect();
    finishVoice();
  }, [finishVoice]);

  const toggleMute = useCallback(async () => {
    const nextMuted = !voiceRef.current.muted;
    setVoice((current) => ({
      ...current,
      muted: nextMuted,
      visual: nextMuted ? "idle" : "listening",
      label: nextMuted ? "Microphone muted" : "Listening",
      detail: nextMuted ? "Tap the microphone to resume" : "Ask a follow-up",
    }));
    await voiceClientRef.current?.enableMic(!nextMuted && voiceRef.current.ready);
  }, []);

  const serviceReady = health?.status !== "not_ready" && health !== null;
  const fullyReady = health?.status === "ready";
  const agentHealth = health?.checks?.find((item) => item.name?.includes("Agent"))?.status || (serviceReady ? "Ready" : "Unavailable");
  const sortedLatencies = [...voiceLatencies].sort((a, b) => a - b);
  const median = sortedLatencies.length
    ? sortedLatencies.length % 2
      ? sortedLatencies[Math.floor(sortedLatencies.length / 2)]
      : (sortedLatencies[sortedLatencies.length / 2 - 1] + sortedLatencies[sortedLatencies.length / 2]) / 2
    : null;
  const diagnostics: DiagnosticsState = useMemo(() => ({
    agentHealth,
    voiceHealth: serviceReady ? (fullyReady ? "Ready" : "Ready locally") : "Unavailable",
    lastLatency: voiceLatencies.length ? `${voiceLatencies.at(-1)!.toFixed(2)}s` : "—",
    medianLatency: median ? `${median.toFixed(2)}s` : "—",
    activities: toolActivity,
  }), [agentHealth, fullyReady, median, serviceReady, toolActivity, voiceLatencies]);

  return {
    phase,
    auth: { message: authMessage, buttonLabel: authButtonLabel, disabled: authDisabled, login, logout },
    user,
    models,
    selectedModel,
    selectModel,
    conversations,
    historyLoading,
    historyError,
    currentConversation,
    selectConversation,
    newConversation,
    messages,
    conversationLoading,
    generating,
    sendPrompt,
    cancelResponse,
    submitAction,
    health,
    serviceReady,
    fullyReady,
    notice,
    voice,
    startVoice,
    endVoice,
    toggleMute,
    audioRef,
    diagnostics,
    modelStatusLabel,
  };
}

function parseSseRecord(record: string): { type: string; id?: string | null; data: DataRecord } | null {
  let type = "message";
  let id: string | null = null;
  const data: string[] = [];
  for (const line of record.split(/\r?\n/)) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("id:")) id = line.slice(3).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try { return { type, id, data: JSON.parse(data.join("\n")) as DataRecord }; } catch { return null; }
}
