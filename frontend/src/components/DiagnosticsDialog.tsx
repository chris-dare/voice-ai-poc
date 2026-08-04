import { Dialog } from "@base-ui/react/dialog";
import { Check, X } from "lucide-react";
import type { DiagnosticsState } from "../types";

export function DiagnosticsDialog({ open, onOpenChange, diagnostics }: { open: boolean; onOpenChange(open: boolean): void; diagnostics: DiagnosticsState }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="dialog-backdrop" />
        <Dialog.Viewport className="dialog-viewport">
          <Dialog.Popup className="diagnostics-dialog glass-surface">
            <header>
              <div><p className="eyebrow">SYSTEM</p><Dialog.Title>Connection details</Dialog.Title></div>
              <Dialog.Close className="icon-button" aria-label="Close diagnostics"><X size={18} /></Dialog.Close>
            </header>
            <Dialog.Description className="sr-only">Current service health, latency, and recent tool activity.</Dialog.Description>
            <div className="diagnostic-grid">
              <Diagnostic label="Agent API" value={diagnostics.agentHealth} />
              <Diagnostic label="Voice" value={diagnostics.voiceHealth} />
              <Diagnostic label="Last turn" value={diagnostics.lastLatency} />
              <Diagnostic label="Median voice latency" value={diagnostics.medianLatency} />
            </div>
            <section className="diagnostic-activity">
              <h3>Recent tool activity</h3>
              {!diagnostics.activities.length && <p>No tool calls in this session.</p>}
              {diagnostics.activities.map((activity) => (
                <div className="diagnostic-event" key={activity.id}>
                  {activity.status === "complete" ? <Check size={13} /> : <i />}
                  <span>{activity.label}</span><small>{activity.status}</small>
                </div>
              ))}
            </section>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Diagnostic({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}
