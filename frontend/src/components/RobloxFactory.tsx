"use client";

import ScriptPromptWorkspace from "./ScriptPromptWorkspace";

type Props = { workspaceId: string; onToast: (message: string, tone?: "brass" | "alert") => void; onPhoneBridge?: () => void };

export default function RobloxFactory({ workspaceId, onToast, onPhoneBridge }: Props) {
  return (
    <ScriptPromptWorkspace
      workspaceId={workspaceId}
      initialNiche="Original Roblox-style blocky stories"
      initialStyle="Original block-built avatars, crisp stud textures, readable silhouettes, playful physics, clean camera framing, no copied game branding or existing character lookalikes."
      seriesName="3D Roblox Story"
      seriesKey="3d_roblox_story"
      onToast={onToast}
      onPhoneBridge={onPhoneBridge}
    />
  );
}
