export function preferredModel(models, preferredId, defaultId) {
  const selectable = (models || []).filter((model) => model.selectable);
  return (
    selectable.find((model) => model.id === preferredId)
    || selectable.find((model) => model.id === defaultId)
    || selectable[0]
    || null
  );
}

export function modelOptionLabel(model) {
  const suffix = model.selectable ? "" : " — unavailable";
  return `${model.display_name || model.id}${suffix}`;
}

export function modelStatusLabel(model) {
  if (!model) return "No model available";
  if (model.status === "available") return "Available";
  if (model.status === "degraded") return "Temporarily degraded";
  if (model.status === "unavailable") return "Unavailable";
  return "Ready to try";
}
