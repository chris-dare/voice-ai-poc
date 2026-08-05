import { ArrowUp, Mic, MicOff, PhoneOff, Square } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { ModelEntry, VoiceState } from "../types";
import { ModelPicker } from "./ModelPicker";

interface ComposerProps {
  models: ModelEntry[];
  selectedModel: ModelEntry | null;
  generating: boolean;
  serviceReady: boolean;
  voice: VoiceState;
  initialPrompt: string;
  onPromptConsumed(): void;
  onSelectModel(model: ModelEntry): void;
  onSend(text: string): void;
  onStop(): void;
  onStartVoice(): void;
  onEndVoice(): void;
  onToggleMute(): void;
}

export function Composer(props: ComposerProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const voiceActive = props.voice.connected || props.voice.visual !== "idle";

  useEffect(() => {
    if (!props.initialPrompt) return;
    setText(props.initialPrompt);
    props.onPromptConsumed();
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }, [props.initialPrompt, props.onPromptConsumed]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [text]);

  const submit = () => {
    if (props.generating && !text.trim()) {
      props.onStop();
      return;
    }
    if (!text.trim() || !props.selectedModel) return;
    const prompt = text;
    setText("");
    props.onSend(prompt);
  };

  return (
    <footer className="composer-zone">
      <motion.div className={`composer ${voiceActive ? "voice-active" : ""}`} layout transition={{ type: "spring", stiffness: 420, damping: 38 }}>
        <AnimatePresence mode="wait" initial={false}>
          {voiceActive ? (
            <motion.div className="voice-composer" key="voice" initial={{ opacity: 0, scale: .985 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .985 }}>
              <VoiceOrb state={props.voice.visual} level={props.voice.micLevel} />
              <div className="voice-label"><strong>{props.voice.label}</strong><span>{props.voice.detail}</span></div>
              <div className="voice-controls">
                <button className={props.voice.muted ? "active" : ""} type="button" onClick={props.onToggleMute} aria-label={props.voice.muted ? "Unmute microphone" : "Mute microphone"}>
                  {props.voice.muted ? <MicOff size={19} /> : <Mic size={19} />}
                </button>
                <button className="end-call" type="button" onClick={props.onEndVoice} aria-label="End voice conversation"><PhoneOff size={19} /></button>
              </div>
            </motion.div>
          ) : (
            <motion.form className="text-composer" key="text" onSubmit={(event) => { event.preventDefault(); submit(); }} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <textarea
                ref={textareaRef}
                rows={1}
                maxLength={16000}
                value={text}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    submit();
                  }
                }}
                placeholder="Ask anything"
                aria-label="Message Voice AI"
              />
              <div className="composer-bottom">
                <ModelPicker models={props.models} selected={props.selectedModel} disabled={props.generating} onSelect={props.onSelectModel} />
                <div className="composer-actions">
                  <button className="composer-action" type="button" onClick={props.onStartVoice} disabled={!props.serviceReady || props.generating || !props.selectedModel} aria-label="Start voice conversation"><Mic size={19} /></button>
                  <button className="send-action" type="submit" disabled={!props.generating && (!text.trim() || !props.selectedModel)} aria-label={props.generating && !text.trim() ? "Stop response" : props.generating ? "Interrupt and send" : "Send message"}>
                    {props.generating && !text.trim() ? <Square size={14} fill="currentColor" /> : <ArrowUp size={19} />}
                  </button>
                </div>
              </div>
            </motion.form>
          )}
        </AnimatePresence>
      </motion.div>
      <p>Voice AI can make mistakes. Check important information.</p>
    </footer>
  );
}

function VoiceOrb({ state, level }: { state: VoiceState["visual"]; level: number }) {
  return (
    <motion.div className={`voice-orb ${state}`} animate={{ scale: 1 + level * .055 }} transition={{ type: "spring", stiffness: 500, damping: 32 }} aria-hidden="true">
      <span /><span /><span />
    </motion.div>
  );
}
