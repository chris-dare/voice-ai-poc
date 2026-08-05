import { Check, CircleAlert, Code2, Globe2, Sigma, Sparkles } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { lazy, Suspense, useEffect, useRef } from "react";
import type { ChatMessage, RequiredAction } from "../types";
import { BrandMark } from "./BrandMark";

const MessageMarkdown = lazy(() => import("./MessageMarkdown"));

const suggestions = [
  { icon: Sparkles, title: "Think through a decision", detail: "Compare options and trade-offs", prompt: "Help me think through a difficult decision by clarifying the constraints and comparing the trade-offs." },
  { icon: Sigma, title: "Work with numbers", detail: "Calculate and verify", prompt: "Calculate the monthly payment on a GHS 120,000 loan at 18% over 5 years and explain the result." },
  { icon: Globe2, title: "Explore what’s current", detail: "Search and synthesise", prompt: "Search the web for today's most important technology news and give me a concise, useful briefing." },
  { icon: Code2, title: "Build something", detail: "Plan, write, and refine", prompt: "Help me turn an idea into a practical implementation plan with clear milestones." },
];

interface ConversationProps {
  userName?: string;
  messages: ChatMessage[];
  loading: boolean;
  generating: boolean;
  onSuggestion(prompt: string): void;
  onAction(messageId: string, responseId: string, action: RequiredAction, decision: "approve" | "reject"): void;
}

export function Conversation({ userName, messages, loading, generating, onSuggestion, onAction }: ConversationProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const firstName = userName?.split(/\s+/)[0];

  useEffect(() => {
    if (!followRef.current) return;
    scrollerRef.current?.scrollTo({ top: scrollerRef.current.scrollHeight, behavior: messages.length > 2 ? "smooth" : "auto" });
  }, [messages]);

  return (
    <section
      className="conversation"
      aria-label="Conversation"
      ref={scrollerRef}
      onScroll={(event) => {
        const element = event.currentTarget;
        followRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
      }}
    >
      {!messages.length && !loading ? (
        <motion.div className="welcome" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <BrandMark size="large" />
          <p className="eyebrow">VOICE AI</p>
          <h1>{firstName ? `What’s on your mind, ${firstName}?` : "What’s on your mind?"}</h1>
          <p className="welcome-copy">Ask naturally. We can reason, research, calculate, write, and build on the conversation together.</p>
          <div className="suggestion-grid">
            {suggestions.map(({ icon: Icon, title, detail, prompt }, index) => (
              <motion.button
                type="button"
                key={title}
                onClick={() => onSuggestion(prompt)}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: .08 + index * .045 }}
                whileHover={{ y: -2 }}
                whileTap={{ scale: .985 }}
              >
                <Icon size={17} />
                <span><strong>{title}</strong><small>{detail}</small></span>
              </motion.button>
            ))}
          </div>
        </motion.div>
      ) : (
        <div className="message-list">
          {loading && <AssistantThinking label="Opening conversation" />}
          <AnimatePresence initial={false}>
            {messages.map((message) => (
              <Message key={message.id} message={message} generating={generating} onAction={onAction} />
            ))}
          </AnimatePresence>
        </div>
      )}
    </section>
  );
}

function Message({ message, generating, onAction }: { message: ChatMessage; generating: boolean; onAction: ConversationProps["onAction"] }) {
  if (message.role === "user") {
    return (
      <motion.article className="message user-message" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }}>
        <div>{message.text}</div>
      </motion.article>
    );
  }
  return (
    <motion.article className="message assistant-message" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }}>
      <BrandMark size="small" />
      <div className="assistant-body">
        {!!message.activities?.length && (
          <div className="tool-row" aria-label="Tool activity">
            {message.activities.map((activity) => (
              <span className={`tool-pill ${activity.status}`} key={activity.id}>
                {activity.status === "complete" ? <Check size={12} /> : <i />}{activity.label}
              </span>
            ))}
          </div>
        )}
        {message.text ? (
          <div className={`markdown ${message.error ? "error" : ""}`}>
            {message.error && <CircleAlert size={17} />}
            <Suspense fallback={<p>{message.text}</p>}>
              <MessageMarkdown>{message.text}</MessageMarkdown>
            </Suspense>
          </div>
        ) : message.pending ? <AssistantThinking label="Thinking" /> : null}
        {message.pending && message.text && <span className="stream-caret" aria-label="Generating response" />}
        {message.requiredAction && message.responseId && (
          <div className="approval-card">
            <strong>{message.requiredAction.title || "Approval required"}</strong>
            <p>{message.requiredAction.description || "Review this action before it continues."}</p>
            <div>
              <button type="button" onClick={() => onAction(message.id, message.responseId!, message.requiredAction!, "reject")} disabled={generating}>Not now</button>
              <button className="approve" type="button" onClick={() => onAction(message.id, message.responseId!, message.requiredAction!, "approve")} disabled={generating}>Approve</button>
            </div>
          </div>
        )}
      </div>
    </motion.article>
  );
}

function AssistantThinking({ label }: { label: string }) {
  return <span className="thinking"><i /><i /><i /><span>{label}</span></span>;
}
