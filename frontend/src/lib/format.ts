import type { ModelEntry } from "../types";

export function modelStatusLabel(model: ModelEntry | null): string {
  if (!model) return "No model available";
  if (model.status === "available") return "Available";
  if (model.status === "degraded") return "Temporarily degraded";
  if (model.status === "unavailable") return "Unavailable";
  return "Ready to try";
}

export function preferredModel(models: ModelEntry[], preferredId?: string | null, defaultId?: string | null): ModelEntry | null {
  const selectable = models.filter((model) => model.selectable);
  return selectable.find((model) => model.id === preferredId)
    || selectable.find((model) => model.id === defaultId)
    || selectable[0]
    || null;
}

export function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "AI";
}

export function readableTool(name = ""): string {
  return name.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function inputText(items: Array<{ content?: Array<{ text?: string }> }> = []): string {
  return items.flatMap((item) => item.content || []).map((content) => content.text || "").join("\n").trim();
}

export function outputText(items: Array<{ content?: Array<{ text?: string }> }> = []): string {
  return items.flatMap((item) => item.content || []).map((content) => content.text || "").join("\n").trim();
}

export function statusMessage(response: { status?: string; error?: { message?: string } }): string {
  if (response.status === "failed") return response.error?.message || "This response failed.";
  if (response.status === "cancelled") return "This response was stopped.";
  if (response.status === "queued" || response.status === "in_progress") return "This response is still running.";
  return "";
}
