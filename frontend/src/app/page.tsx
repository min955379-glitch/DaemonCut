"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Activity, AudioLines, BarChart3, BookOpen, Box, CalendarClock, ChevronDown,
  CircleHelp, Clapperboard, CloudUpload, Cpu, Gauge, HardDrive, KeyRound, Layers3,
  MonitorCog, Plus, Radio, RefreshCw, ScrollText, Settings2, ShieldCheck,
  Sparkles, TrendingUp, Tv, Video, Wifi, X
} from "lucide-react";
import ApiKeysPanel from "@/components/ApiKeysPanel";
import AnalyticsPanel from "@/components/AnalyticsPanel";
import BatchScheduler from "@/components/BatchScheduler";
import CartoonFactory from "@/components/CartoonFactory";
import DirectorChat from "@/components/DirectorChat";
import NicheStudio from "@/components/NicheStudio";
import RobloxFactory from "@/components/RobloxFactory";
import StreamClipper from "@/components/StreamClipper";
import TerminalDrawer from "@/components/TerminalDrawer";
import TrendScout from "@/components/TrendScout";
import VoiceModeSwitch, { type VoiceMode } from "@/components/VoiceModeSwitch";
import { apiGet, apiPost, platformStatusList, type Channel, type Episode, type PlatformStatus, type PlatformStatusPayload, type Workspace } from "@/lib/api";

type TabKey = "cartoon" | "roblox" | "niche" | "director" | "clipper" | "batch" | "trends" | "analytics";
type Tab = { id: TabKey; label: string; short: string; icon: typeof Clapperboard; description: string };
type Health = { status: string; timestamp?: string; database?: Record<string, number>; scheduler?: { running: boolean } };
type Hardware = { status: string; hardware?: { gpu?: string; gpu_vram_gb?: number; ram_gb?: number; cpu_logical_threads?: number; video_encoder?: string; whisper_backend?: string }; drive?: { configured: boolean; mounted: boolean; folder_name?: string | null } };
type Niche = { id: string; name: string; series_bible?: Record<string, unknown>; blueprint?: Record<string, unknown> };
type Toast = { message: string; tone: "brass" | "alert" };

const TABS: Tab[] = [
  { id: "cartoon", label: "3D Cartoon Factory", short: "Cartoon", icon: Clapperboard, description: "Original episodic animation and continuity prompts" },
  { id: "roblox", label: "3D Roblox Story", short: "Roblox", icon: Box, description: "Block-built story beats and original avatar direction" },
  { id: "niche", label: "Add New Niche", short: "Niche Studio", icon: Sparkles, description: "Build a strategy and series bible for any genre" },
  { id: "director", label: "Director Copilot", short: "Director", icon: AudioLines, description: "Dictate, inspect, and refine the next cut" },
  { id: "clipper", label: "PeakCut Clipper", short: "PeakCut", icon: Video, description: "Score highlights and render vertical clips" },
  { id: "batch", label: "Batch Scheduler", short: "Scheduler", icon: CalendarClock, description: "Review seven-episode batches and publish destinations" },
  { id: "trends", label: "Trend Scout", short: "Trends", icon: TrendingUp, description: "Review research leads across the trend feed" },
  { id: "analytics", label: "Analytics & Health", short: "Analytics", icon: BarChart3, description: "Inspect queue, platform, workstation, and Drive state" }
];

const DEFAULT_BIBLES: Array<{ title: string; description: string; icon: typeof BookOpen; bible: Record<string, unknown> }> = [
  { title: "Breezy & Mal", description: "Original 3D cartoon series", icon: Sparkles, bible: { series: "Breezy & Mal", format: "Short-form, gentle-comedy stories", continuity: "Keep character silhouettes, facial proportions, wardrobe, and recurring props consistent.", storytelling: "Give each episode one clear problem, a playful escalation, and a warm resolution.", visual_language: "Readable expressive 3D animation; warm practical light; original props and locations." } },
  { title: "3D Roblox Story", description: "Original block-built stories", icon: Box, bible: { series: "3D Roblox Story", format: "Original block-built short stories", continuity: "Use original blocky avatars and stud textures; preserve silhouette, outfit, and prop continuity.", storytelling: "Use clear spatial staging, playful physics, and a short visual payoff.", visual_language: "Crisp block geometry and clean camera framing; no copied game branding or character designs." } }
];

export default function HomePage() {
  const [activeTabId, setActiveTab] = useState<TabKey>("cartoon");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [hardware, setHardware] = useState<Hardware | null>(null);
  const [platforms, setPlatforms] = useState<PlatformStatus[]>([]);
  const [queue, setQueue] = useState<Episode[]>([]);
  const [niches, setNiches] = useState<Niche[]>([]);
  const [voiceMode, setVoiceMode] = useState<VoiceMode>("auto_cast");
  const [showApiVault, setShowApiVault] = useState(false);
  const [showBible, setShowBible] = useState(false);
  const [showChannelForm, setShowChannelForm] = useState(false);
  const [channelPlatform, setChannelPlatform] = useState("youtube");
  const [channelId, setChannelId] = useState("");
  const [channelName, setChannelName] = useState("");
  const [selectedBible, setSelectedBible] = useState<{ title: string; data: Record<string, unknown> } | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const activeWorkspace = useMemo(() => workspaces.find((item) => item.workspace_id === workspaceId) ?? null, [workspaces, workspaceId]);
  const activeTab = TABS.find((item) => item.id === activeTabId) ?? TABS[0];

  const notify = useCallback((message: string, tone: Toast["tone"] = "brass") => {
    setToast({ message, tone });
  }, []);

  const refreshNiches = useCallback(() => {
    if (!workspaceId) return;
    void apiGet<{ niches: Niche[] }>(`/api/niches?workspace_id=${encodeURIComponent(workspaceId)}`)
      .then((result) => setNiches(result.niches ?? []))
      .catch(() => undefined);
  }, [workspaceId]);

  useEffect(() => {
    const storedVoice = window.localStorage.getItem("daemoncut-voice-mode");
    if (storedVoice === "auto_cast" || storedVoice === "cloned_voice" || storedVoice === "manual_override") setVoiceMode(storedVoice);
    let alive = true;
    apiGet<{ items: Workspace[] }>("/api/workspaces")
      .then((result) => {
        if (!alive) return;
        const items = result.items ?? [];
        setWorkspaces(items);
        const storedWorkspace = window.localStorage.getItem("daemoncut-workspace-id");
        const selected = items.find((item) => item.workspace_id === storedWorkspace) ?? items[0];
        if (selected) setWorkspaceId(selected.workspace_id);
      })
      .catch((reason: unknown) => { if (alive) setError(reason instanceof Error ? reason.message : "DaemonCut API is not responding."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setHealth(null); setHardware(null); setPlatforms([]); setQueue([]); setNiches([]);
      return;
    }
    window.localStorage.setItem("daemoncut-workspace-id", workspaceId);
    let alive = true;
    const load = async () => {
      try {
        const [healthResult, hardwareResult, publisherResult, queueResult, nicheResult] = await Promise.all([
          apiGet<Health>("/api/health"),
          apiGet<Hardware>("/api/system"),
          apiGet<{ platforms: PlatformStatusPayload }>("/api/publish/status"),
          apiGet<{ items: Episode[] }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/queue?limit=100`),
          apiGet<{ niches: Niche[] }>(`/api/niches?workspace_id=${encodeURIComponent(workspaceId)}`)
        ]);
        if (!alive) return;
        setHealth(healthResult);
        setHardware(hardwareResult);
        setPlatforms(platformStatusList(publisherResult.platforms));
        setQueue(queueResult.items ?? []);
        setNiches(nicheResult.niches ?? []);
        setError("");
      } catch (reason) {
        if (alive) setError(reason instanceof Error ? reason.message : "Workspace status could not be refreshed.");
      }
    };
    void load();
    const interval = window.setInterval(() => { void load(); }, 30_000);
    return () => { alive = false; window.clearInterval(interval); };
  }, [workspaceId]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4_500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!workspaceId) return;
    let alive = true;
    apiGet<{ niches: Niche[] }>(`/api/niches?workspace_id=${encodeURIComponent(workspaceId)}`)
      .then((result) => { if (alive) setNiches(result.niches ?? []); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [workspaceId, activeTab]);

  async function createWorkspace() {
    setBusy(true);
    setError("");
    try {
      const result = await apiPost<{ workspace: Workspace }>("/api/workspaces", {
        name: "DaemonCut Development",
        timezone_name: "Asia/Karachi",
        default_platform: "youtube",
        settings: { storage_policy: "drive_media_local_code", language: "English" }
      });
      const workspace = result.workspace;
      setWorkspaces((current) => current.some((item) => item.workspace_id === workspace.workspace_id) ? current : [...current, workspace]);
      setWorkspaceId(workspace.workspace_id);
      window.localStorage.setItem("daemoncut-workspace-id", workspace.workspace_id);
      notify("DaemonCut Development workspace created.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Workspace could not be created."); }
    finally { setBusy(false); }
  }

  async function createChannel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!workspaceId || !channelId.trim()) return;
    setBusy(true);
    try {
      const result = await apiPost<{ channel: Channel }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/channels`, {
        platform: channelPlatform,
        external_channel_id: channelId.trim(),
        display_name: channelName.trim() || channelPlatform,
        autopublish: true
      });
      setWorkspaces((current) => current.map((workspace) => workspace.workspace_id === workspaceId ? { ...workspace, channels: [...(workspace.channels ?? []), result.channel] } : workspace));
      setChannelId(""); setChannelName(""); setShowChannelForm(false);
      notify(`${channelPlatform} destination added.`);
    } catch (reason) { notify(reason instanceof Error ? reason.message : "Channel could not be added.", "alert"); }
    finally { setBusy(false); }
  }

  function openPhoneBridge() {
    const popup = window.open("/bridge", "daemoncut-bridge", "popup,width=720,height=900");
    if (!popup) notify("Allow pop-ups to open the phone upload bridge.", "alert");
  }

  function changeVoiceMode(mode: VoiceMode) {
    setVoiceMode(mode);
    window.localStorage.setItem("daemoncut-voice-mode", mode);
    notify(`Voice routing set to ${mode.replaceAll("_", " ")}.`);
  }

  function openBible(title: string, data: Record<string, unknown>) {
    setSelectedBible({ title, data });
    setShowBible(true);
  }

  const CurrentTabIcon = activeTab.icon;
  const configuredPlatforms = platforms.filter((item) => item.connected || item.fallback_available).length;
  const readyQueue = queue.filter((item) => ["queued", "approved", "ready", "scheduled", "publishing"].includes(item.status.toLowerCase())).length;

  return (
    <main className="console-shell">
      <header className="top-bar">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded border border-brass bg-panel text-brass"><MonitorCog size={18} /></div>
          <div className="min-w-0"><p className="eyebrow">DaemonCut / local creator system</p><h1 className="truncate text-sm font-semibold tracking-wide">MATTE OBSIDIAN <span className="text-brass">/</span> BRASS CONSOLE</h1></div>
        </div>
        <div className="top-actions flex items-center gap-2">
          <span className={`tag ${health?.status === "active" ? "tag-brass" : "tag-alert"}`}><span className={`status-led ${health?.status === "active" ? "" : "status-led-alert"}`} />API {health?.status?.toUpperCase() ?? (loading ? "CONNECTING" : "OFFLINE")}</span>
          <button className="btn" type="button" onClick={() => setShowApiVault(true)}><KeyRound size={14} /> Key Vault</button>
          <button className="btn btn-primary" type="button" onClick={openPhoneBridge}><CloudUpload size={14} /> Phone Bridge</button>
        </div>
      </header>

      {error && <div className="mx-3 mt-3 border-l-2 border-crimson bg-crimson/10 px-3 py-2 text-xs text-crimson" role="alert">{error}</div>}

      <div className="console-grid">
        <aside className="zone-rail" aria-label="Workspace and local controls">
          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">01 / Workspace</p><h2 className="mt-1 text-sm font-semibold">Studio routing</h2></div><button className="btn btn-quiet min-h-7 px-2 py-1" type="button" title="Refresh workspace list" onClick={() => window.location.reload()}><RefreshCw size={13} /></button></div>
            <div className="panel-body space-y-3">
              {workspaces.length ? <label className="field-label">Active workspace
                <span className="relative"><select className="select appearance-none pr-8" value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)}>{workspaces.map((workspace) => <option key={workspace.workspace_id} value={workspace.workspace_id}>{workspace.name}</option>)}</select><ChevronDown size={14} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-brass" /></span>
              </label> : <div className="empty-state min-h-24 text-xs">{loading ? "Loading workspaces…" : "Create a local workspace to begin production."}</div>}
              {!workspaces.length && !loading && <button className="btn btn-primary w-full" type="button" onClick={() => void createWorkspace()} disabled={busy}><Plus size={14} /> Create Development Workspace</button>}
              {activeWorkspace && <>
                <div className="flex items-center justify-between"><span className="micro-label">CHANNEL DESTINATIONS</span><button className="btn btn-quiet min-h-7 px-2 py-1" type="button" onClick={() => setShowChannelForm((current) => !current)}><Plus size={13} /> Add</button></div>
                {activeWorkspace.channels?.length ? <div className="flex flex-wrap gap-1">{activeWorkspace.channels.map((channel) => <span className="tag tag-steel" key={channel.channel_id} title={channel.external_channel_id}><Radio size={10} />{channel.platform} · {channel.display_name || channel.external_channel_id}</span>)}</div> : <p className="text-[10px] text-parchment/65">No channel records yet. OAuth publishing can still be configured independently.</p>}
                {showChannelForm && <form className="tile space-y-2" onSubmit={(event) => void createChannel(event)}>
                  <label className="field-label">Platform<select className="select" value={channelPlatform} onChange={(event) => setChannelPlatform(event.target.value)}>{["youtube", "tiktok", "facebook", "instagram"].map((platform) => <option value={platform} key={platform}>{platform}</option>)}</select></label>
                  <label className="field-label">Provider channel / page ID<input className="field" value={channelId} onChange={(event) => setChannelId(event.target.value)} required maxLength={255} /></label>
                  <label className="field-label">Display name<input className="field" value={channelName} onChange={(event) => setChannelName(event.target.value)} maxLength={160} /></label>
                  <div className="flex justify-end gap-2"><button className="btn min-h-8 px-2" type="button" onClick={() => setShowChannelForm(false)}>Cancel</button><button className="btn btn-primary min-h-8 px-2" type="submit" disabled={busy}>Save channel</button></div>
                </form>}
              </>}
              {activeWorkspace && <div className="tile"><div className="flex items-center justify-between"><span className="micro-label">STORAGE POLICY</span><HardDrive size={13} className="text-steel" /></div><p className="mt-2 text-xs">Code here. Media and renders in <span className="text-brass">DaemonCut Development</span> Drive.</p></div>}
            </div>
          </section>

          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">02 / Persistent canon</p><h2 className="mt-1 text-sm font-semibold">Series Bible</h2></div><BookOpen size={15} className="text-brass" /></div>
            <div className="panel-body space-y-2">
              {DEFAULT_BIBLES.map((item) => { const Icon = item.icon; return <button key={item.title} className="tile flex w-full items-center gap-2 text-left" type="button" onClick={() => openBible(item.title, item.bible)}><Icon size={14} className="text-brass" /><span className="min-w-0 flex-1"><span className="block text-xs font-semibold">{item.title}</span><span className="text-[10px] text-parchment/65">{item.description}</span></span><ScrollText size={12} className="text-steel" /></button>; })}
              {niches.map((niche) => <button key={niche.id} className="tile flex w-full items-center gap-2 text-left" type="button" onClick={() => openBible(niche.name, niche.series_bible ?? { series: niche.name, blueprint: niche.blueprint ?? {} })}><Layers3 size={14} className="text-steel" /><span className="min-w-0 flex-1"><span className="block truncate text-xs font-semibold">{niche.name}</span><span className="text-[10px] text-parchment/65">Custom niche / saved bible</span></span></button>)}
              <p className="text-[10px] text-parchment/65">Canon panels persist across factory tabs. New niche bibles are stored as lightweight workspace metadata.</p>
            </div>
          </section>

          <VoiceModeSwitch value={voiceMode} onChange={changeVoiceMode} />

          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">System quick links</p><h2 className="mt-1 text-sm font-semibold">Local tools</h2></div><Settings2 size={15} className="text-steel" /></div>
            <div className="panel-body grid grid-cols-2 gap-2">
              <button className="btn" type="button" onClick={() => setShowApiVault(true)}><KeyRound size={13} /> Key Vault</button>
              <button className="btn" type="button" onClick={openPhoneBridge}><Wifi size={13} /> Phone Bridge</button>
              <a className="btn" href="/docs" target="_blank" rel="noreferrer"><CircleHelp size={13} /> API Docs</a>
              <button className="btn" type="button" onClick={() => setActiveTab("analytics")}><Gauge size={13} /> Health</button>
            </div>
          </section>
        </aside>

        <section className="zone-main" aria-label="Production workspace">
          <div className="panel overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
              <div className="min-w-0"><p className="eyebrow">{activeWorkspace?.name ?? "Workspace not selected"}</p><h2 className="mt-1 flex items-center gap-2 text-base font-semibold"><CurrentTabIcon size={16} className="text-brass" />{activeTab.label}</h2><p className="mt-1 text-[10px] text-parchment/65">{activeTab.description}</p></div>
              <span className="tag tag-steel"><span className="status-led status-led-blue" />LOCAL-FIRST / {voiceMode.replaceAll("_", " ").toUpperCase()}</span>
            </div>
            <nav className="tab-row" aria-label="Production modules">
              {TABS.map((tab) => { const Icon = tab.icon; const selected = tab.id === activeTab.id; return <button className={`tab-button ${selected ? "tab-button-active" : ""}`} aria-current={selected ? "page" : undefined} key={tab.id} type="button" onClick={() => setActiveTab(tab.id)}><Icon size={13} />{tab.short}</button>; })}
            </nav>
          </div>

          {!workspaceId && !loading ? <section className="panel"><div className="panel-body"><div className="empty-state"><div><MonitorCog size={32} className="mx-auto mb-3 text-brass" /><h3 className="text-base font-semibold">Your local studio is ready to configure</h3><p className="mt-2 text-xs">Create a workspace to persist episodes, niche bibles, channel destinations, and the publishing queue.</p><button className="btn btn-primary mt-4" type="button" onClick={() => void createWorkspace()} disabled={busy}><Plus size={14} /> Create workspace</button></div></div></div></section> : <>
            {activeTab.id === "cartoon" && <CartoonFactory workspaceId={workspaceId} onToast={notify} onPhoneBridge={openPhoneBridge} />}
            {activeTab.id === "roblox" && <RobloxFactory workspaceId={workspaceId} onToast={notify} onPhoneBridge={openPhoneBridge} />}
            {activeTab.id === "niche" && <NicheStudio workspaceId={workspaceId} onToast={notify} onSaved={refreshNiches} />}
            {activeTab.id === "director" && <DirectorChat onToast={notify} />}
            {activeTab.id === "clipper" && <StreamClipper workspaceId={workspaceId} onToast={notify} />}
            {activeTab.id === "batch" && <BatchScheduler workspaceId={workspaceId} onToast={notify} />}
            {activeTab.id === "trends" && <TrendScout workspaceId={workspaceId} />}
            {activeTab.id === "analytics" && <AnalyticsPanel workspaceId={workspaceId} />}
          </>}
        </section>

        <aside className="zone-inspector" aria-label="Live console status">
          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">Telemetry</p><h2 className="mt-1 text-sm font-semibold">System inspector</h2></div><MonitorCog size={15} className="text-steel" /></div>
            <div className="panel-body space-y-1">
              <InspectorRow label="API service" value={health?.status?.toUpperCase() ?? (loading ? "CONNECTING" : "OFFLINE")} tone={health?.status === "active" ? "brass" : "alert"} />
              <InspectorRow label="Workspace" value={activeWorkspace?.slug ?? "None"} tone={activeWorkspace ? "steel" : "alert"} />
              <InspectorRow label="Drive mount" value={hardware?.drive?.mounted ? "READY" : hardware?.drive?.configured ? "UNMOUNTED" : "NOT SET"} tone={hardware?.drive?.mounted ? "brass" : "alert"} />
              <InspectorRow label="GPU" value={hardware?.hardware?.gpu ?? "Not detected"} tone="steel" />
              <InspectorRow label="Video encoder" value={hardware?.hardware?.video_encoder ?? "n/a"} tone="steel" />
              <InspectorRow label="Queue ready" value={String(readyQueue)} tone={readyQueue ? "brass" : "steel"} />
              <InspectorRow label="Publish destinations" value={`${configuredPlatforms} / ${platforms.length}`} tone={configuredPlatforms ? "brass" : "steel"} />
              <InspectorRow label="Scheduler" value={health?.scheduler?.running ? "RUNNING" : "STANDBY"} tone={health?.scheduler?.running ? "brass" : "steel"} />
            </div>
          </section>

          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">Publishing adapters</p><h2 className="mt-1 text-sm font-semibold">Platform isolation</h2></div><ShieldCheck size={15} className="text-brass" /></div>
            <div className="panel-body space-y-1">
              {platforms.map((platform) => <div key={platform.platform} className="inspector-row"><span className="flex items-center gap-2"><span className={`status-led ${platform.connected ? "" : platform.fallback_available ? "status-led-blue" : "status-led-alert"}`} />{platform.platform}</span><span className={`tag ${platform.connected ? "tag-brass" : platform.fallback_available ? "tag-steel" : "tag-alert"}`}>{platform.connected ? "CONNECTED" : platform.fallback_available ? "DRIVE" : platform.configured ? "AUTH" : "OFF"}</span></div>)}
              {!platforms.length && <p className="text-xs text-parchment/65">Publisher status unavailable.</p>}
              <p className="pt-2 text-[10px] text-parchment/65">Meta setup is optional. Each platform can be authorized and published independently.</p>
              <button className="btn w-full" type="button" onClick={() => setShowApiVault(true)}><KeyRound size={13} /> Manage credentials</button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">Recent queue</p><h2 className="mt-1 text-sm font-semibold">Production snapshot</h2></div><Activity size={15} className="text-brass" /></div>
            <div className="panel-body space-y-2">
              {queue.slice(0, 5).map((episode) => <div className="tile" key={episode.episode_id}><div className="flex items-center justify-between gap-2"><span className="truncate text-xs font-semibold">{episode.title}</span><span className={`tag ${episode.status === "failed" ? "tag-alert" : episode.status === "published" ? "tag-brass" : "tag-steel"}`}>{episode.status}</span></div><p className="mt-1 text-[10px] text-parchment/65">{episode.scheduled_for ? new Date(episode.scheduled_for).toLocaleString("en-PK", { timeZone: "Asia/Karachi" }) : "Not scheduled"}</p></div>)}
              {!queue.length && <div className="empty-state min-h-20 text-xs">No queued episodes.</div>}
              <button className="btn w-full" type="button" onClick={() => setActiveTab("batch")}><CalendarClock size={13} /> Open batch scheduler</button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head"><div><p className="eyebrow">Workstation profile</p><h2 className="mt-1 text-sm font-semibold">Hardware</h2></div><Cpu size={15} className="text-steel" /></div>
            <div className="panel-body space-y-1"><InspectorRow label="CPU threads" value={String(hardware?.hardware?.cpu_logical_threads ?? "—")} tone="steel" /><InspectorRow label="Memory" value={`${hardware?.hardware?.ram_gb ?? "—"} GiB`} tone="steel" /><InspectorRow label="Whisper backend" value={hardware?.hardware?.whisper_backend ?? "n/a"} tone="steel" /><InspectorRow label="Runtime DB" value={`${health?.database?.episodes ?? 0} episodes`} tone="steel" /><p className="pt-2 text-[10px] text-parchment/65">Heavy models, media, and renders stay in configured Drive storage or local runtime state.</p></div>
          </section>

          <section className="panel">
            <div className="panel-body flex items-start gap-3"><div className="grid h-8 w-8 shrink-0 place-items-center rounded border border-steel text-steel"><Tv size={15} /></div><div><p className="micro-label">Console scope</p><p className="mt-1 text-xs">Original creative work · local-first production · review-before-publish workflow.</p><p className="mt-2 text-[10px] text-parchment/65">Workspace metadata in SQLite; OAuth token vault in <span className="mono">.daemoncut/</span>; media in Drive.</p></div></div>
          </section>
        </aside>
      </div>

      <div className="mx-3 mb-3"><TerminalDrawer /></div>
      {toast && <div className={`toast ${toast.tone === "alert" ? "border-crimson" : ""}`} role="status" aria-live="polite">{toast.message}</div>}
      {showApiVault && <ApiKeysPanel onClose={() => setShowApiVault(false)} onToast={notify} />}
      {showBible && selectedBible && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowBible(false); }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="bible-title"><div className="panel-head"><div><p className="eyebrow">Persistent series canon</p><h2 id="bible-title" className="mt-1 text-lg font-semibold">{selectedBible.title}</h2></div><button className="btn btn-quiet" type="button" onClick={() => setShowBible(false)} aria-label="Close series bible"><X size={16} /></button></div><div className="panel-body space-y-3"><pre className="code-block">{JSON.stringify(selectedBible.data, null, 2)}</pre><p className="text-[10px] text-parchment/65">Keep character details, environment, and continuity rules consistent. Reference image prompts are not character-limited.</p></div></section></div>}
    </main>
  );
}

function InspectorRow({ label, value, tone }: { label: string; value: string; tone: "brass" | "steel" | "alert" }) {
  const tag = tone === "brass" ? "tag-brass" : tone === "alert" ? "tag-alert" : "tag-steel";
  const led = tone === "brass" ? "" : tone === "alert" ? "status-led-alert" : "status-led-blue";
  return <div className="inspector-row"><span className="flex items-center gap-2 text-parchment/65"><span className={`status-led ${led}`} />{label}</span><span className={`tag ${tag}`}>{value}</span></div>;
}
