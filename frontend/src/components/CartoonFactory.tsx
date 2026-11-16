"use client";

import ScriptPromptWorkspace from "./ScriptPromptWorkspace";

type Props = { workspaceId: string; onToast: (message: string, tone?: "brass" | "alert") => void; onPhoneBridge?: () => void };

export default function CartoonFactory({ workspaceId, onToast, onPhoneBridge }: Props) {
  return (
    <ScriptPromptWorkspace
      workspaceId={workspaceId}
      initialNiche="Original 3D cartoon stories"
      initialStyle="Readable expressive 3D animation, warm practical light, purposeful camera movement, original props, kind humor."
      seriesName="Breezy & Mal"
      seriesKey="breezy_and_mal"
      onToast={onToast}
      onPhoneBridge={onPhoneBridge}
    />
  );
}
