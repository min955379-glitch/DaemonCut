"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CalendarClock, Check, CirclePlay, LoaderCircle, RefreshCw, Rocket, ScanSearch, WandSparkles } from "lucide-react";
import { apiGet, apiPost, platformStatusList, type Episode, type PlatformStatus, type PlatformStatusPayload } from "@/lib/api";

type Props = { workspaceId: string; onToast: (message: string, tone?: "brass" | "alert") => void };
type DraftEpisode = { topic: string; title: string; hook: string; script: string };
type Variant = { label: string; text: string };
type VariantSet = { hooks: Variant[]; titles: Variant[] };
type BatchEnvelope = { batch: { batch_id: string; batch_date: string; episodes: Episode[] } };
type QueueEnvelope = { items: Episode[] };
type BatchesEnvelope = { items: Array<{ batch_id: string; batch_date: string; status: string; episode_count: number }> };
type SchedulerEnvelope = { scheduler: { running: boolean; timezone: string; peak_times: string[]; last_error?: string | null; last_result?: unknown } };
type PublishEnvelope = { results: Array<{ platform: string; status: string; message?: string; remote_id?: string }> };
type PlatformEnvelope = { platforms: PlatformStatusPayload };

const emptyDrafts = (): DraftEpisode[] => Array.from({ length: 7 }, () => ({ topic: "", title: "", hook: "", script: "" }));
const PLATFORM_NAMES: Record<string, string> = { youtube: "YouTube Shorts", tiktok: "TikTok", facebook: "Facebook / Drive Export", instagram: "Instagram Reels" };

export default function BatchScheduler({ workspaceId, onToast }: Props) {
  const [drafts, setDrafts] = useState<DraftEpisode[]>(emptyDrafts);
  const [batchDate, setBatchDate] = useState(nextSaturdayDate);
  const [variants, setVariants] = useState<Record<number, VariantSet>>({});
  const [queue, setQueue] = useState<Episode[]>([]);
  const [batches, setBatches] = useState<BatchesEnvelope["items"]>([]);
  const [scheduler, setScheduler] = useState<SchedulerEnvelope["scheduler"] | null>(null);
  const [platforms, setPlatforms] = useState<PlatformStatus[]>([]);
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>([]);
  const [scan, setScan] = useState<Record<string, unknown> | null>(null);
  const [publishResults, setPublishResults] = useState<Record<string, PublishEnvelope["results"]>>({});
  const [busy, setBusy] = useState(false);
  const [busyEpisode, setBusyEpisode] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!workspaceId) return;
    setError("");
    try {
      const [queueResult, batchResult, schedulerResult, platformResult] = await Promise.all([
        apiGet<QueueEnvelope>(`/api/workspaces/${encodeURIComponent(workspaceId)}/queue?limit=100`),
        apiGet<BatchesEnvelope>(`/api/workspaces/${encodeURIComponent(workspaceId)}/batches?limit=20`),
        apiGet<SchedulerEnvelope>("/api/scheduler/status"),
        apiGet<PlatformEnvelope>("/api/publish/status")
      ]);
      setQueue(queueResult.items ?? []);
      setBatches(batchResult.items ?? []);
      setScheduler(schedulerResult.scheduler);
      const platformItems = platformStatusList(platformResult.platforms);
      setPlatforms(platformItems);
      setSelectedPlatforms((current) => current.length ? current : platformItems.filter((item) => item.connected || item.fallback_available).map((item) => item.platform));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scheduler state could not be loaded.");
    }
  }, [workspaceId]);

  useEffect(() => { void load(); }, [load]);

  const canQueue = useMemo(() => drafts.length === 7 && drafts.every((item) => item.topic.trim() || item.title.trim()), [drafts]);

  function updateDraft(index: number, field: keyof DraftEpisode, value: string) {
    setDrafts((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item));
  }

  async function generateVariants(index: number) {
    const draft = drafts[index];
    const topic = draft?.topic.trim() || draft?.title.trim();
    if (!topic) { onToast(`Add a topic for episode ${index + 1} first.`, "alert"); return; }
    setBusyEpisode(`variants-${index}`);
    try {
      const result = await apiPost<{ variants: VariantSet }>("/api/trends/variants", { topic, context: draft.script, audience: "Short-form audience", platform: "YouTube Shorts", language: "English", use_ai: true });
      setVariants((current) => ({ ...current, [index]: result.variants }));
      onToast(`A/B/C hooks and titles are ready for episode ${index + 1}.`);
    } catch (reason) { onToast(reason instanceof Error ? reason.message : "Could not generate variants.", "alert"); }
    finally { setBusyEpisode(""); }
  }

  async function runScan() {
    if (!workspaceId) { setError("Select a workspace before scanning."); return; }
    if (!canQueue) { setError("Enter a topic or title for all seven episodes before scanning."); return; }
    setBusy(true);
    setError("");
    try {
      const result = await apiPost<{ scan: Record<string, unknown> }>("/api/trends/scan-batch", {
        workspace_id: workspaceId,
        threshold: 0.72,
        episodes: drafts.map((item) => ({ topic: item.topic.trim(), title: item.title.trim(), hook: item.hook.trim(), script: item.script.trim() }))
      });
      setScan(result.scan);
      setNotice("Seven-episode anti-repetition scan completed. Review flags before queueing.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Anti-repetition scan failed."); }
    finally { setBusy(false); }
  }

  async function createBatch() {
    if (!workspaceId) { setError("Select a workspace before queueing."); return; }
    if (!isSaturday(batchDate)) { setError("Choose Saturday as the batch date; the daily slots then continue through Friday."); return; }
    if (!canQueue) { setError("Enter a topic or title for all seven episodes."); return; }
    setBusy(true);
    setError("");
    try {
      const episodes = drafts.map((item, index) => {
        const day = addDays(batchDate, index);
        const time = index % 2 === 0 ? "21:30" : "00:30";
        const scheduled = new Date(`${day}T${time}:00+05:00`).toISOString();
        return { ...item, topic: item.topic.trim() || item.title.trim(), title: item.title.trim() || item.topic.trim(), hook: item.hook.trim(), script: item.script.trim(), scheduled_for: scheduled };
      });
      const response = await apiPost<BatchEnvelope>(`/api/workspaces/${encodeURIComponent(workspaceId)}/batches/saturday`, { batch_date: batchDate, episodes });
      setNotice(`Batch ${response.batch.batch_id.slice(0, 8)} queued for ${response.batch.batch_date}; slots use Asia/Karachi time.`);
      setScan(null);
      setDrafts(emptyDrafts());
      onToast("Saturday seven-episode batch queued.");
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Saturday batch could not be queued."); }
    finally { setBusy(false); }
  }

  async function publishEpisode(episode: Episode) {
    if (!selectedPlatforms.length) { onToast("Choose at least one independent publishing platform.", "alert"); return; }
    setBusyEpisode(episode.episode_id);
    setError("");
    try {
      const result = await apiPost<PublishEnvelope>(`/api/publish/episode/${encodeURIComponent(episode.episode_id)}`, {
        platforms: selectedPlatforms,
        idempotency_key: `console:${episode.episode_id}:${selectedPlatforms.slice().sort().join("-")}`
      });
      setPublishResults((current) => ({ ...current, [episode.episode_id]: result.results ?? [] }));
      onToast(`Publish run finished for ${episode.title}.`);
      await load();
    } catch (reason) { onToast(reason instanceof Error ? reason.message : "Episode publish request failed.", "alert"); }
    finally { setBusyEpisode(""); }
  }

  async function schedulerAction(action: "run" | "start" | "stop") {
    setBusy(true);
    setError("");
    try {
      if (action === "run") await apiPost("/api/scheduler/run", { wait_for_connectivity: false, cleanup_scratch: false });
      else if (action === "start") await apiPost("/api/scheduler/start", { boot_run: false });
      else await apiPost("/api/scheduler/stop", {});
      await load();
      onToast(action === "run" ? "Scheduler cycle completed." : `PKT scheduler ${action === "start" ? "started" : "stopped"}.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Scheduler action failed."); }
    finally { setBusy(false); }
  }

  function togglePlatform(platform: string) {
    setSelectedPlatforms((current) => current.includes(platform) ? current.filter((item) => item !== platform) : [...current, platform]);
  }

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">07 / Production calendar</p><h2 className="mt-1 text-lg font-semibold">Saturday 7-episode batch</h2></div><span className={`tag ${scheduler?.running ? "tag-brass" : "tag-steel"}`}><span className={`status-led ${scheduler?.running ? "" : "status-led-blue"}`} />{scheduler?.running ? "PKT SCHEDULER ACTIVE" : "SCHEDULER STANDBY"}</span></div>
        <div className="panel-body space-y-4">
          {error && <p role="alert" className="border-l-2 border-crimson bg-crimson/10 px-3 py-2 text-xs text-crimson">{error}</p>}
          {notice && <p className="border-l-2 border-brass bg-brass/10 px-3 py-2 text-xs text-brass">{notice}</p>}
          <div className="grid gap-3 md:grid-cols-[1fr_auto] md:items-end">
            <label className="field-label">Batch Saturday / Asia-Karachi schedule
              <input className="field" type="date" value={batchDate} onChange={(event) => setBatchDate(event.target.value)} />
            </label>
            <div className="flex flex-wrap gap-2"><button className="btn" type="button" onClick={() => void runScan()} disabled={busy || !canQueue}><ScanSearch size={14} /> Scan 7 for repetition</button><button className="btn btn-primary" type="button" onClick={() => void createBatch()} disabled={busy || !canQueue}><CalendarClock size={14} /> Queue batch</button></div>
          </div>
          <div className="tile grid gap-2 sm:grid-cols-2"><p className="text-xs"><span className="mono text-brass">21:30 PKT</span> / evening publishing window</p><p className="text-xs"><span className="mono text-brass">00:30 PKT</span> / after-midnight window</p><p className="text-[10px] text-parchment/65 sm:col-span-2">Slots alternate each day from Saturday through Friday. Meta can fall back to a Drive ready-to-post bundle; YouTube and TikTok remain independent.</p></div>
          <div className="grid gap-3 xl:grid-cols-2">
            {drafts.map((item, index) => <article key={index} className="tile space-y-2">
              <div className="flex items-center justify-between"><h3 className="micro-label">EPISODE {String(index + 1).padStart(2, "0")}</h3><span className="tag tag-steel">{index % 2 === 0 ? "21:30 PKT" : "00:30 PKT"} · +{index} day</span></div>
              <label className="field-label">Topic<input className="field" value={item.topic} onChange={(event) => updateDraft(index, "topic", event.target.value)} maxLength={500} placeholder={`Original episode ${index + 1} topic`} /></label>
              <label className="field-label">Title<input className="field" value={item.title} onChange={(event) => updateDraft(index, "title", event.target.value)} maxLength={200} placeholder="Optional; defaults to topic" /></label>
              <label className="field-label">Hook<input className="field" value={item.hook} onChange={(event) => updateDraft(index, "hook", event.target.value)} maxLength={2000} /></label>
              <label className="field-label">Script notes<textarea className="textarea min-h-[64px]" value={item.script} onChange={(event) => updateDraft(index, "script", event.target.value)} maxLength={100000} /></label>
              <div className="flex flex-wrap items-center justify-between gap-2"><button className="btn min-h-8 px-2" type="button" onClick={() => void generateVariants(index)} disabled={busyEpisode === `variants-${index}`}><WandSparkles size={12} /> {busyEpisode === `variants-${index}` ? "Working…" : "Generate A/B/C"}</button>
                {variants[index] && <div className="grid min-w-0 flex-1 gap-2 sm:grid-cols-2"><label className="field-label">Hook<select className="select min-h-8 py-1" value={item.hook} onChange={(event) => updateDraft(index, "hook", event.target.value)}><option value="">Choose hook</option>{variants[index].hooks.map((variant) => <option key={variant.label} value={variant.text}>{variant.label} · {variant.text}</option>)}</select></label><label className="field-label">Title<select className="select min-h-8 py-1" value={item.title} onChange={(event) => updateDraft(index, "title", event.target.value)}><option value="">Choose title</option>{variants[index].titles.map((variant) => <option key={variant.label} value={variant.text}>{variant.label} · {variant.text}</option>)}</select></label></div>}
              </div>
            </article>)}
          </div>
          {scan && <section className="tile"><div className="flex items-center gap-2"><Check size={14} className="text-brass" /><p className="micro-label">ANTI-REPETITION REVIEW</p></div><pre className="code-block mt-2 max-h-72">{JSON.stringify(scan, null, 2)}</pre></section>}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Independent publisher</p><h3 className="mt-1 font-semibold">Choose destinations per publish run</h3></div><button className="btn min-h-8 px-2" type="button" onClick={() => void load()} disabled={busy}><RefreshCw size={12} /> Refresh</button></div>
        <div className="panel-body space-y-3">
          <div className="flex flex-wrap gap-2">{platforms.map((platform) => <label key={platform.platform} className={`tag cursor-pointer ${selectedPlatforms.includes(platform.platform) ? "tag-brass" : ""}`} title={platform.message}><input type="checkbox" className="accent-brass" checked={selectedPlatforms.includes(platform.platform)} onChange={() => togglePlatform(platform.platform)} />{PLATFORM_NAMES[platform.platform] ?? platform.platform}{platform.fallback_available && !platform.connected ? " · DRIVE" : ""}</label>)}</div>
          <div className="grid gap-2">
            {!queue.length && <div className="empty-state min-h-24">No queued episodes yet. Build and scan a Saturday batch above.</div>}
            {queue.slice(0, 30).map((episode) => {
              const hasMedia = Boolean(episode.metadata?.video_path || episode.metadata?.rendered_path || episode.metadata?.media_path);
              return <article key={episode.episode_id} className="tile flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h4 className="truncate text-sm font-semibold">{episode.title}</h4><span className="tag tag-steel">{episode.status.toUpperCase()}</span>{!hasMedia && <span className="tag tag-alert">MEDIA NEEDED</span>}</div><p className="mt-1 text-[10px] text-parchment/65">{episode.scheduled_for ? new Date(episode.scheduled_for).toLocaleString("en-PK", { timeZone: "Asia/Karachi" }) + " PKT" : "Not scheduled"} · {episode.episode_id.slice(0, 8)}</p>{publishResults[episode.episode_id] && <div className="mt-2 flex flex-wrap gap-1">{publishResults[episode.episode_id].map((result, index) => <span key={`${result.platform}-${index}`} className={`tag ${result.status === "failed" ? "tag-alert" : "tag-brass"}`}>{result.platform}: {result.status}</span>)}</div>}</div>
                <button className="btn btn-primary" type="button" title={hasMedia ? "Publish to selected destinations" : "Render a video before publishing"} onClick={() => void publishEpisode(episode)} disabled={busyEpisode === episode.episode_id || !selectedPlatforms.length || !hasMedia}><Rocket size={13} /> {busyEpisode === episode.episode_id ? <LoaderCircle size={13} className="animate-spin" /> : hasMedia ? "Publish selected" : "Add media first"}</button>
              </article>;
            })}
          </div>
          {!!batches.length && <div><p className="micro-label mb-2">RECENT BATCHES</p><div className="flex flex-wrap gap-2">{batches.slice(0, 8).map((batch) => <span className="tag" key={batch.batch_id}>{batch.batch_date} · {batch.episode_count}/7 · {batch.status}</span>)}</div></div>}
          <div className="separator" />
          <div className="flex flex-wrap items-center justify-between gap-3"><div><p className="micro-label">SCHEDULER CONTROLS</p><p className="mt-1 text-[10px] text-parchment/65">Timezone: {scheduler?.timezone ?? "Asia/Karachi"} · PKT windows: {scheduler?.peak_times?.join(" / ") ?? "21:30 / 00:30"}</p></div><div className="flex flex-wrap gap-2"><button className="btn" type="button" onClick={() => void schedulerAction("run")} disabled={busy}><CirclePlay size={13} /> Run once</button>{scheduler?.running ? <button className="btn btn-danger" type="button" onClick={() => void schedulerAction("stop")} disabled={busy}>Stop background</button> : <button className="btn" type="button" onClick={() => void schedulerAction("start")} disabled={busy}>Start background</button>}</div></div>
        </div>
      </section>
    </div>
  );
}

function nextSaturdayDate() {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = (6 - next.getDay() + 7) % 7 || 7;
  next.setDate(next.getDate() + days);
  return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}-${String(next.getDate()).padStart(2, "0")}`;
}

function isSaturday(value: string) {
  return value.length === 10 && new Date(`${value}T12:00:00Z`).getUTCDay() === 6;
}

function addDays(value: string, days: number) {
  const day = new Date(`${value}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() + days);
  return day.toISOString().slice(0, 10);
}
