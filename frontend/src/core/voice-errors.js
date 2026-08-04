const MICROPHONE_ERROR_NAMES = new Set([
  "NotAllowedError",
  "NotFoundError",
  "NotReadableError",
  "OverconstrainedError",
  "SecurityError",
]);

export function voiceErrorPresentation(error, connected) {
  const name = error?.name || error?.cause?.name;
  const message = String(error?.message || "").toLowerCase();
  const microphoneFailure = MICROPHONE_ERROR_NAMES.has(name)
    || message.includes("microphone permission")
    || message.includes("media device");

  if (microphoneFailure) {
    return {
      state: "error",
      label: "Microphone unavailable",
      detail: "Check microphone permission and try again",
      recoverable: false,
    };
  }
  if (connected) {
    return {
      state: "listening",
      label: "Voice interrupted",
      detail: "I'm still listening—please try that again",
      recoverable: true,
    };
  }
  return {
    state: "error",
    label: "Voice unavailable",
    detail: "Could not open the audio connection",
    recoverable: false,
  };
}
