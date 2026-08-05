import { CircleAlert, Info, Menu, Plus } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { AuthScreen } from "./components/AuthScreen";
import { BrandMark } from "./components/BrandMark";
import { Composer } from "./components/Composer";
import { Conversation } from "./components/Conversation";
import { DiagnosticsDialog } from "./components/DiagnosticsDialog";
import { Sidebar } from "./components/Sidebar";
import { useVoiceAI } from "./hooks/useVoiceAI";

export default function App() {
  const app = useVoiceAI();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [suggestedPrompt, setSuggestedPrompt] = useState("");

  if (app.phase === "boot") {
    return (
      <main className="boot-screen">
        <BrandMark size="large" />
        <span className="boot-label">Opening Voice AI</span>
      </main>
    );
  }

  if (app.phase === "auth") {
    return <AuthScreen message={app.auth.message} buttonLabel={app.auth.buttonLabel} disabled={app.auth.disabled} onLogin={() => void app.auth.login()} />;
  }

  const serviceLabel = app.fullyReady ? "All systems ready" : app.serviceReady ? "Ready on this device" : "Services unavailable";
  const name = app.user?.name || app.user?.nickname;

  return (
    <main className="application">
      <Sidebar
        open={sidebarOpen}
        user={app.user}
        conversations={app.conversations}
        currentConversation={app.currentConversation}
        loading={app.historyLoading}
        error={app.historyError}
        onClose={() => setSidebarOpen(false)}
        onNew={() => { void app.newConversation(); setSidebarOpen(false); }}
        onSelect={(conversation) => { void app.selectConversation(conversation); setSidebarOpen(false); }}
        onDiagnostics={() => setDiagnosticsOpen(true)}
        onLogout={() => void app.auth.logout()}
      />

      <section className="workspace">
        <header className="topbar">
          <button className="icon-button mobile-control" type="button" onClick={() => setSidebarOpen(true)} aria-label="Open conversation history"><Menu size={19} /></button>
          <div className="workspace-title"><strong>Voice AI</strong><span><i className={app.serviceReady ? "ready" : "error"} />{serviceLabel}</span></div>
          <button className="status-button desktop-control" type="button" onClick={() => setDiagnosticsOpen(true)}><span>{app.diagnostics.lastLatency === "—" ? "Ready" : `${app.diagnostics.lastLatency} last turn`}</span><Info size={16} /></button>
          <button className="icon-button mobile-control" type="button" onClick={() => void app.newConversation()} aria-label="New conversation"><Plus size={19} /></button>
        </header>

        <AnimatePresence>
          {app.notice && (
            <motion.div className={`notice ${app.notice.kind}`} role="status" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }}>
              <CircleAlert size={15} /><span>{app.notice.message}</span>
            </motion.div>
          )}
        </AnimatePresence>

        <Conversation
          userName={name}
          messages={app.messages}
          loading={app.conversationLoading}
          generating={app.generating}
          onSuggestion={setSuggestedPrompt}
          onAction={(messageId, responseId, action, decision) => void app.submitAction(messageId, responseId, action, decision)}
        />

        <Composer
          models={app.models}
          selectedModel={app.selectedModel}
          generating={app.generating}
          serviceReady={app.serviceReady}
          voice={app.voice}
          initialPrompt={suggestedPrompt}
          onPromptConsumed={() => setSuggestedPrompt("")}
          onSelectModel={app.selectModel}
          onSend={(text) => void app.sendPrompt(text)}
          onStop={() => void app.cancelResponse()}
          onStartVoice={() => void app.startVoice()}
          onEndVoice={() => void app.endVoice()}
          onToggleMute={() => void app.toggleMute()}
        />
      </section>

      <DiagnosticsDialog open={diagnosticsOpen} onOpenChange={setDiagnosticsOpen} diagnostics={app.diagnostics} />
      <audio ref={app.audioRef} autoPlay playsInline />
    </main>
  );
}
