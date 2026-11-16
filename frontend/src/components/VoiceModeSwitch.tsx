"use client";

import { Mic, Mic2, SlidersHorizontal } from "lucide-react";

export type VoiceMode = "auto_cast" | "cloned_voice" | "manual_override";
type Props = { value: VoiceMode; onChange: (mode: VoiceMode) => void };

const OPTIONS: Array<{ value: VoiceMode; label: string; description: string; icon: typeof Mic }> = [
  { value: "auto_cast", label: "Auto-Cast", description: "Choose a voice profile per character.", icon: Mic },
  { value: "cloned_voice", label: "My Cloned Voice", description: "Use a locally configured cloned-voice profile.", icon: Mic2 },
  { value: "manual_override", label: "Manual Override", description: "Bring your own audio path for each line.", icon: SlidersHorizontal }
];

export default function VoiceModeSwitch({ value, onChange }: Props) {
  return (
    <section aria-label="Voice mode" className="panel">
      <div className="panel-head"><div><p className="eyebrow">Audio routing</p><h2 className="mt-1 text-sm font-semibold">Voice mode</h2></div></div>
      <div className="panel-body space-y-2">
        {OPTIONS.map((option) => {
          const Icon = option.icon;
          const active = value === option.value;
          return <button key={option.value} type="button" aria-pressed={active} className={`tile flex w-full items-center gap-3 text-left ${active ? "border-brass" : ""}`} onClick={() => onChange(option.value)}>
            <span className={`grid h-8 w-8 shrink-0 place-items-center rounded border ${active ? "border-brass text-brass" : "border-border text-steel"}`}><Icon size={15} /></span>
            <span className="min-w-0 flex-1"><span className="block text-xs font-semibold">{option.label}</span><span className="mt-1 block text-[10px] text-parchment/65">{option.description}</span></span>
            {active && <span className="status-led" aria-label="Selected" />}
          </button>;
        })}
        <p className="text-[10px] text-parchment/65">Voice choice applies to future audio synthesis. Cloned voice profiles remain local.</p>
      </div>
    </section>
  );
}
