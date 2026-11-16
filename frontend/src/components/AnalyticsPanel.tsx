"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Activity, BarChart3, Database, HardDrive, RefreshCw, ServerCog } from "lucide-react";
import { apiGet, platformStatusList, type Episode, type PlatformStatus, type PlatformStatusPayload } from "@/lib/api";

type Props = { workspaceId: string };
type QueueEnvelope = { items: Episode[] };
type PlatformEnvelope = { platforms: PlatformStatusPayload };
type Health = { status: string; timestamp?: string; database?: Record<string, number>; scheduler?: { running: boolean } };
type SystemEnvelope = { status: string; hardware?: { cpu_logical_threads?: number; gpu?: string; ram_gb?: number; video_encoder?: string }; drive?: { configured: boolean; mounted: boolean; folder_name?: string | null } };

const ORDER = ["draft", "queued", "approved", "rendering", "ready", "scheduled", "publishing", "published", "failed", "archived"];

export default function AnalyticsPanel({ workspaceId }: Props) {
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [platforms, setPlatforms] = useState<PlatformStatus[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [system, setSystem] = useState<SystemEnvelope | null>(null);
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const tasks = [
        apiGet<Health>("/api/health"),
        apiGet<SystemEnvelope>("/api/system"),
        apiGet<PlatformEnvelope>("/api/publish/status")
      ] as const;
      const [healthResult, systemResult, publisherResult] = await Promise.all(tasks);
      setHealth(healthResult);
      setSystem(systemResult);
      setPlatforms(platformStatusList(publisherResult.platforms));
      if (workspaceId) {
        const queueResult = await apiGet<QueueEnvelope>(`/api/workspaces/${encodeURIComponent(workspaceId)}/queue?limit=2000`);
        setEpisodes(queueResult.items ?? []);
      } else setEpisodes([]);
      setUpdatedAt(new Date().toISOString());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Analytics could not be loaded.");
    } finally { setBusy(false); }
  }, [workspaceId]);

  useEffect(() => { void load(); }, [load]);

  const totals = episodes.reduce<Record<string, number>>((result, episode) => {
    const status = episode.status.toLowerCase();
    result[status] = (result[status] ?? 0) + 1;
    return result;
  }, {});
  const activeCount = (totals.queued ?? 0) + (totals.approved ?? 0) + (totals.ready ?? 0) + (totals.scheduled ?? 0) + (totals.publishing ?? 0);
  const completion = episodes.length ? Math.round(((totals.published ?? 0) / episodes.length) * 100) : 0;

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">08 / Studio telemetry</p><h2 className="mt-1 text-lg font-semibold">Analytics &amp; health</h2></div><button className="btn min-h-8 px-2" type="button" onClick={() => void load()} disabled={busy}><RefreshCw size={12} className={busy ? "animate-spin" : ""} /> Refresh</button></div>
        <div className="panel-body space-y-4">
          {error && <p role="alert" className="border-l-2 border-crimson px-3 py-2 text-xs text-crimson">{error}</p>}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Episodes" value={episodes.length} icon={<Activity size={15} />} note="Current workspace queue" />
            <Metric label="Active queue" value={activeCount} icon={<BarChart3 size={15} />} note="Queued, approved, scheduled, publishing" />
            <Metric label="Published" value={`${completion}%`} icon={<Database size={15} />} note={`${totals.published ?? 0} of ${episodes.length} episodes`} />
            <Metric label="Failed" value={totals.failed ?? 0} icon={<ServerCog size={15} />} note="Review publish or render results" alert={(totals.failed ?? 0) > 0} />
          </div>

          <section className="tile space-y-3">
            <div className="flex items-center justify-between"><div><p className="micro-label">Queue distribution</p><h3 className="mt-1 font-semibold">Episode status</h3></div><span className="tag tag-steel">{episodes.length} total</span></div>
            {episodes.length ? <div className="grid gap-2 sm:grid-cols-2">
              {ORDER.filter((status) => totals[status]).map((status) => <div className="flex items-center gap-3" key={status}><span className="w-24 text-[10px] uppercase tracking-wider text-parchment/65">{status}</span><div className="h-2 flex-1 overflow-hidden rounded bg-[#121316]"><div className={`h-full ${status === "failed" ? "bg-crimson" : ["published", "ready"].includes(status) ? "bg-brass" : "bg-steel"}`} style={{ width: `${Math.max(3, ((totals[status] ?? 0) / episodes.length) * 100)}%` }} /></div><span className="mono w-8 text-right text-xs">{totals[status]}</span></div>)}
            </div> : <p className="text-xs text-parchment/65">No episodes yet; queue a script or seven-episode batch to establish baseline metrics.</p>}
          </section>

          <section className="tile space-y-3">
            <div className="flex items-center justify-between"><div><p className="micro-label">Independent provider status</p><h3 className="mt-1 font-semibold">Publishing destinations</h3></div><span className="tag tag-brass">No cross-platform dependency</span></div>
            <div className="grid gap-2 sm:grid-cols-2">
              {platforms.map((platform) => <div key={platform.platform} className="inspector-row"><span className="flex items-center gap-2"><span className={`status-led ${platform.connected ? "" : platform.fallback_available ? "status-led-blue" : "status-led-alert"}`} />{platform.platform}</span><span className={`tag ${platform.connected ? "tag-brass" : platform.fallback_available ? "tag-steel" : "tag-alert"}`}>{platform.connected ? "CONNECTED" : platform.fallback_available ? "READY-TO-POST" : "NOT CONFIGURED"}</span></div>)}
            </div>
            <p className="text-[10px] text-parchment/65">A missing Meta configuration never blocks YouTube or TikTok. Meta may export a Drive handoff bundle.</p>
          </section>

          <section className="tile space-y-3">
            <div className="flex items-center gap-2"><HardDrive size={14} className="text-steel" /><p className="micro-label">Local workstation</p></div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div><p className="text-xs font-semibold">API / {health?.status ?? "unknown"}</p><p className="mt-1 text-[10px] text-parchment/65">{health?.database?.episodes ?? 0} local episodes · Scheduler {health?.scheduler?.running ? "active" : "standby"}</p></div>
              <div><p className="text-xs font-semibold">Drive / {system?.drive?.mounted ? "mounted" : system?.drive?.configured ? "not mounted" : "not configured"}</p><p className="mt-1 text-[10px] text-parchment/65">{system?.drive?.folder_name ?? "Drive root hidden"} · {system?.hardware?.gpu ?? "GPU n/a"}</p></div>
              <div><p className="text-xs font-semibold">Encoder / {system?.hardware?.video_encoder ?? "n/a"}</p><p className="mt-1 text-[10px] text-parchment/65">{system?.hardware?.cpu_logical_threads ?? "?"} logical threads · {system?.hardware?.ram_gb ?? "?"} GiB RAM</p></div>
              <div><p className="text-xs font-semibold">Workspace timezone / Asia-Karachi</p><p className="mt-1 text-[10px] text-parchment/65">Last refreshed {updatedAt ? new Date(updatedAt).toLocaleTimeString() : "—"}</p></div>
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value, icon, note, alert = false }: { label: string; value: string | number; icon: ReactNode; note: string; alert?: boolean }) {
  return <article className="tile"><div className="flex items-center justify-between"><span className="micro-label">{label}</span><span className={alert ? "text-crimson" : "text-brass"}>{icon}</span></div><p className={`mt-3 font-mono text-2xl font-semibold ${alert ? "text-crimson" : "text-parchment"}`}>{value}</p><p className="mt-1 text-[10px] text-parchment/65">{note}</p></article>;
}
