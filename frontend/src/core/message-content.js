export function contentSegments(value) {
  const text = String(value || "");
  const segments = [];
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g;
  let position = 0;
  for (const match of text.matchAll(fence)) {
    if (match.index > position) {
      segments.push({ type: "text", text: text.slice(position, match.index) });
    }
    segments.push({
      type: "code",
      language: match[1].trim(),
      text: match[2].replace(/\n$/, ""),
    });
    position = match.index + match[0].length;
  }
  if (position < text.length) segments.push({ type: "text", text: text.slice(position) });
  return segments.length ? segments : [{ type: "text", text }];
}
