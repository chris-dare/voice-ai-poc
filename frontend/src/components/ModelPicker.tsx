import { Menu } from "@base-ui/react/menu";
import { Check, ChevronDown } from "lucide-react";
import { modelStatusLabel } from "../lib/format";
import type { ModelEntry } from "../types";

interface ModelPickerProps {
  models: ModelEntry[];
  selected: ModelEntry | null;
  disabled: boolean;
  onSelect(model: ModelEntry): void;
}

export function ModelPicker({ models, selected, disabled, onSelect }: ModelPickerProps) {
  return (
    <Menu.Root>
      <Menu.Trigger className="model-trigger" disabled={disabled || !models.length} aria-label="Choose AI model">
        <span className={`availability-dot ${selected?.status || "unavailable"}`} />
        <span>{selected?.display_name || "No model available"}</span>
        <ChevronDown size={14} />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner className="model-positioner" side="top" align="start" sideOffset={10} collisionPadding={12}>
          <Menu.Popup className="model-menu glass-surface">
            <div className="model-menu-heading"><strong>Choose a model</strong><small>For your next message</small></div>
            <Menu.RadioGroup value={selected?.id || ""} onValueChange={(value) => {
              const model = models.find((item) => item.id === value);
              if (model?.selectable) onSelect(model);
            }}>
              {models.map((model) => (
                <Menu.RadioItem className="model-item" value={model.id} disabled={!model.selectable} key={model.id} title={model.detail || modelStatusLabel(model)}>
                  <span className={`availability-dot ${model.status}`} />
                  <span className="model-copy">
                    <strong>{model.display_name || model.id}</strong>
                    <small>{[model.provider, modelStatusLabel(model)].filter(Boolean).join(" · ")}</small>
                  </span>
                  <Menu.RadioItemIndicator className="model-check"><Check size={16} /></Menu.RadioItemIndicator>
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
            <p className="model-menu-foot">{selected?.detail || modelStatusLabel(selected)}</p>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
