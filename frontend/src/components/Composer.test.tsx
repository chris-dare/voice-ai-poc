import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ModelEntry, VoiceState } from "../types";
import { Composer } from "./Composer";

const model: ModelEntry = {
  id: "provider:model",
  display_name: "Available model",
  provider: "provider",
  status: "available",
  selectable: true,
};

const voice: VoiceState = {
  connected: false,
  ready: false,
  muted: false,
  visual: "idle",
  label: "Voice",
  detail: "Ready when you are",
  micLevel: 0,
};

describe("Composer", () => {
  it("accepts typed input and submits it", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <Composer
        models={[model]}
        selectedModel={model}
        generating={false}
        serviceReady
        voice={voice}
        initialPrompt=""
        onPromptConsumed={vi.fn()}
        onSelectModel={vi.fn()}
        onSend={onSend}
        onStop={vi.fn()}
        onStartVoice={vi.fn()}
        onEndVoice={vi.fn()}
        onToggleMute={vi.fn()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Message Voice AI" }) as HTMLTextAreaElement;
    await user.type(input, "Can you hear me?");

    expect(input.value).toBe("Can you hear me?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSend).toHaveBeenCalledWith("Can you hear me?");
  });
});
