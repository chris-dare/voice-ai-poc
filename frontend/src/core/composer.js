export function promptAction(generating, value) {
  const text = value.trim();
  if (generating) {
    return text ? { type: "interrupt", text } : { type: "stop", text: "" };
  }
  return text ? { type: "send", text } : { type: "ignore", text: "" };
}
