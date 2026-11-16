"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, Clipboard, Clapperboard, LoaderCircle, RefreshCw, Smartphone, Sparkles, WandSparkles } from "lucide-react";
import { apiGet, apiPost, MAX_YT_CREATE_CHARS, type PromptScene, type ScriptDraft } from "@/lib/api";
import { useFileDrop } from "@/lib/useFileDrop";

const REFERENCE_KEYS = ["E1", "E2", "E3", "E3B"] as const;
type Toast = (message: string, tone?: "brass" | "alert") => void;

type ScriptEnvelope = { script: ScriptDraft };
type TrendEnvelope = { items: Array<{ topic: string; trend_score?: number; country_names?: string[]; repeated_recently?: boolean }> };
type EpisodeEnvelope = { episode: { episode_id: string; title: string } };
type RenderEnvelope = { render: { path: string; duration_seconds: number } };

export type ScriptPromptWorkspaceProps = {
  workspaceId: string;
  initialNiche: string;
  initialStyle: string;
  seriesName?: string;
  seriesKey?: string;
  onToast: Toast;
  onPhoneBridge?: () => void;
};

function PromptCard({ label, value, onChange, limit, onCopy }: { label: string; value: string; onChange?: (next: string) => void; limit?: number; onCopy: () => void }) {
  const count = value.length;
  const over = typeof limit === "number" && count > limit;
  return (
    <article className="prompt-card">
      <div className="prompt-card-head">
        <span className="eyebrow">{label}</span>
        <div className="flex items-center gap-2">
          {typeof limit === "number" && <span className={`counter ${over ? "counter-over" : ""}`}>{count} / {limit}</span>}
          <button className="btn btn-quiet min-h-0 px-2 py-1" type="button" onClick={onCopy} aria-label={`Copy ${label}`}><Clipboard size={13} /> Copy</button>
        </div>
      </div>
      <textarea
        className="prompt-text"
        value={value}
        onChange={onChange ? (event) => onChange(event.target.value.slice(0, limit ?? 100_000)) : undefined}
        readOnly={!onChange}
        maxLength={limit}
        spellCheck={false}
        aria-label={`${label} prompt`}
      />
      {over && <p className="px-3 pb-2 text-[11px] text-crimson">This scene prompt exceeds the YouTube Create limit.</p>}
    </article>
  );
}

export default function ScriptPromptWorkspace({ workspaceId, initialNiche, initialStyle, seriesName, seriesKey, onToast, onPhoneBridge }: ScriptPromptWorkspaceProps) {
  const [niche, setNiche] = useState(initialNiche);
  const [topic, setTopic] = useState("");
  const [audience, setAudience] = useState("General short-form audience");
  const [duration, setDuration] = useState("45");
  const [sceneCount, setSceneCount] = useState("5");
  const [style, setStyle] = useState(initialStyle);
  const [trends, setTrends] = useState<TrendEnvelope["items"]>([]);
  const [draft, setDraft] = useState<ScriptDraft | null>(null);
  const [references, setReferences] = useState<Record<string, string>>({});
  const [videoPath, setVideoPath] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");
  const [result, setResult] = useState("");

  const fileDrop = useFileDrop({
    onDrop: (path) => setVideoPath(path),
    onToast: (message, kind) => onToast(message, kind === "alert" ? "alert" : "brass"),
  });

  useEffect(() => {
    let alive = true;
    apiGet<TrendEnvelope>(`/api/trends?limit=10${workspaceId ? `&workspace_id=${encodeURIComponent(workspaceId)}` : ""}`)
      .then((payload) => { if (alive) setTrends(payload.items ?? []); })
      .catch(() => { if (alive) setTrends([]); });
    return () => { alive = false; };
  }, [workspaceId]);

  useEffect(() => setNiche(initialNiche), [initialNiche]);
  useEffect(() => setStyle(initialStyle), [initialStyle]);

  const scenes = useMemo(() => draft?.scenes ?? [], [draft]);
  const promptsValid = scenes.every((scene) => typeof scene.video_prompt === "string" && scene.video_prompt.length <= MAX_YT_CREATE_CHARS);

  async function copyText(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      window.setTimeout(() => setCopied(""), 1300);
    } catch {
      onToast("Clipboard access was denied by the browser.", "alert");
    }
  }

  async function generate() {
    if (!topic.trim()) { onToast("Enter a topic or choose a trend first.", "alert"); return; }
    setBusy(true);
    setResult("");
    try {
      const response = await apiPost<ScriptEnvelope>("/api/studio/scripts", {
        niche,
        topic,
        audience,
        duration_seconds: Number(duration) || 45,
        style,
        language: "English",
        series_name: seriesKey,
        scene_count: Number(sceneCount) || 5,
        include_reference_prompts: true,
        character_description: seriesName === "Breezy & Mal" ? "Breezy and Mal, preserving the local series-bible character designs." : "Original characters designed for this episode.",
        environment_description: "A consistent, readable environment with original props and no third-party marks."
      });
      setDraft(response.script);
      setReferences(response.script.reference_prompts ?? {});
      onToast("Script and reference prompts are ready.");
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Script generation failed.", "alert");
    } finally {
      setBusy(false);
    }
  }

  function updateScene(index: number, text: string) {
    setDraft((previous) => {
      if (!previous) return previous;
      const nextScenes = previous.scenes.map((scene, current) => current === index ? { ...scene, video_prompt: text.slice(0, MAX_YT_CREATE_CHARS) } : scene);
      return { ...previous, scenes: nextScenes };
    });
  }

  function buildEpisodeMetadata(path?: string) {
    return {
      ...(path ? { video_path: path } : {}),
      ...(references.E1 ? { reference_prompts: references } : {}),
      scene_prompts: scenes.map((scene) => scene.video_prompt),
      render_state: path ? "rendered" : "needs_media"
    };
  }

  async function queueEpisode(renderFirst: boolean) {
    if (!workspaceId) { onToast("Create or select a workspace before queueing an episode.", "alert"); return; }
    if (!draft) { onToast("Generate a script before queueing the episode.", "alert"); return; }
    if (!promptsValid) { onToast(`Every scene prompt must stay within ${MAX_YT_CREATE_CHARS} characters.`, "alert"); return; }
    if (renderFirst && !videoPath.trim()) { onToast("Choose a rendered scene clip from the Drive workspace first.", "alert"); return; }
    if (!renderFirst && scheduledFor) { onToast("Add a rendered Drive clip before assigning a publish time.", "alert"); return; }
    setBusy(true);
    setResult("");
    try {
      let renderedPath: string | undefined;
      if (renderFirst) {
        const rendered = await apiPost<RenderEnvelope>("/api/studio/video/render", {
          clips: [{ path: videoPath.trim(), start_seconds: 0, framing_mode: "whole_blur", include_audio: true, label: draft.title }],
          resolution: "1080p",
          fps: 60,
          transition: "fade",
          transition_duration: 0
        });
        renderedPath = rendered.render.path;
      }
      const scheduleValue = scheduledFor ? new Date(scheduledFor).toISOString() : null;
      const queued = await apiPost<EpisodeEnvelope>(`/api/workspaces/${encodeURIComponent(workspaceId)}/episodes`, {
        topic: topic.trim(),
        title: draft.title || topic.trim(),
        hook: draft.hook || "",
        script: draft.script || "",
        scheduled_for: renderFirst ? scheduleValue : null,
        status: renderFirst ? (scheduleValue ? "scheduled" : "approved") : "draft",
        metadata: buildEpisodeMetadata(renderedPath)
      });
      const message = renderFirst ? `Rendered and queued ${queued.episode.title}.` : `Queued ${queued.episode.title} as a draft for media. `;
      setResult(`${message} Episode ${queued.episode.episode_id.slice(0, 8)}.`);
      onToast(message);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Episode could not be queued.", "alert");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-12px" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <section className="panel">
        <div className="panel-head">
          <div><p className="eyebrow">01 / Story production</p><h2 className="mt-1 text-lg font-semibold">{seriesName ?? "Short-form script studio"}</h2></div>
          <div className="flex flex-wrap items-center gap-2"><span className="tag tag-steel"><Sparkles size={12} /> Scene prompts ≤ {MAX_YT_CREATE_CHARS}</span>{onPhoneBridge && <button className="btn min-h-8 px-2" type="button" onClick={onPhoneBridge}><Smartphone size={12} /> Phone upload</button>}</div>
        </div>
        <div className="panel-body grid gap-3 md:grid-cols-2">
          <label className="field-label">Niche / series
            <input className="field" value={niche} onChange={(event) => setNiche(event.target.value)} maxLength={160} />
          </label>
          <label className="field-label">Audience
            <input className="field" value={audience} onChange={(event) => setAudience(event.target.value)} maxLength={200} />
          </label>
          <label className="field-label md:col-span-2">Episode topic
            <input className="field" value={topic} onChange={(event) => setTopic(event.target.value)} maxLength={1000} placeholder="A small problem with an inventive solution" />
          </label>
          <label className="field-label">Visual direction
            <textarea className="textarea min-h-[70px]" value={style} onChange={(event) => setStyle(event.target.value)} maxLength={500} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="field-label">Duration seconds<input className="field" type="number" min="10" max="3600" value={duration} onChange={(event) => setDuration(event.target.value)} /></label>
            <label className="field-label">Scene count<input className="field" type="number" min="1" max="40" value={sceneCount} onChange={(event) => setSceneCount(event.target.value)} /></label>
          </div>
          <div className="md:col-span-2">
            <p className="micro-label mb-2">Trend Scout / recent topics</p>
            <div className="flex flex-wrap gap-2">
              {trends.slice(0, 6).map((trend, index) => (
                <button key={`${trend.topic}-${index}`} type="button" className="tag hover:border-brass" onClick={() => setTopic(trend.topic)} title={trend.repeated_recently ? "Topic is similar to recent work" : "Use trend topic"}>
                  {trend.topic}<span className="text-brass">{trend.trend_score ?? "—"}</span>
                </button>
              ))}
              {!trends.length && <span className="text-[11px] text-parchment/65">Trend feed unavailable; enter an original topic.</span>}
            </div>
          </div>
          <div className="md:col-span-2 flex flex-wrap items-center justify-between gap-3">
            <span className="text-[11px] text-parchment/65">Reference image prompts remain uncapped. Only scene video prompts have a character ceiling.</span>
            <button className="btn btn-primary" type="button" onClick={generate} disabled={busy}><WandSparkles size={14} /> {busy ? <LoaderCircle className="animate-spin" size={14} /> : null} Generate script</button>
          </div>
        </div>
      </section>

      {draft && <>
        <section className="panel">
          <div className="panel-head"><div><p className="eyebrow">02 / Episode prompts</p><h3 className="mt-1 text-base font-semibold">{draft.title}</h3></div><button className="btn" type="button" onClick={generate} disabled={busy}><RefreshCw size={13} /> Regenerate</button></div>
          <div className="panel-body space-y-3">
            <div className="grid gap-3 md:grid-cols-2">
              <div className="tile"><p className="micro-label">Hook</p><p className="mt-2 text-sm">{draft.hook}</p></div>
              <div className="tile"><p className="micro-label">Script / narration</p><p className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap text-sm">{draft.script}</p></div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {REFERENCE_KEYS.map((key) => {
                const value = references[key] ?? "";
                return <PromptCard key={key} label={`Reference ${key}`} value={value} onCopy={() => void copyText(key, value)} />;
              })}
            </div>
            <div className="scene-grid">
              {scenes.map((scene: PromptScene, index: number) => (
                <PromptCard key={scene.scene_number ?? index} label={`Scene ${scene.scene_number ?? index + 1} / video prompt`} value={scene.video_prompt} limit={MAX_YT_CREATE_CHARS} onChange={(value) => updateScene(index, value)} onCopy={() => void copyText(`scene-${index}`, scene.video_prompt)} />
              ))}
            </div>
            {!promptsValid && <p className="text-sm text-crimson">A scene prompt exceeds the {MAX_YT_CREATE_CHARS}-character limit.</p>}
            <div className="separator" />
            <div className="grid gap-3 md:grid-cols-2">
              <div className={`rounded-lg border p-3 transition-colors ${fileDrop.isDragging ? "border-brass bg-brass/5" : "border-parchment/15"}`} {...fileDrop.dragHandlers}>
                <label className="field-label flex items-center justify-between gap-2">Rendered scene clip path in Drive
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-brass">drag &amp; drop a file</span>
                </label>
                <input className="field font-mono" value={videoPath} onChange={(event) => setVideoPath(event.target.value)} placeholder="G:\\My Drive\\DaemonCut Development\\source_media\\scene.mp4" />
              </div>
              <label className="field-label">Optional publish time / local workstation time
                <input className="field" type="datetime-local" value={scheduledFor} onChange={(event) => setScheduledFor(event.target.value)} />
              </label>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-[11px] text-parchment/65">Queueing a prompt-only episode leaves it waiting for a render. Add a Drive clip to render and queue in one action.</p>
              <div className="flex flex-wrap gap-2">
                <button className="btn" type="button" onClick={() => void queueEpisode(false)} disabled={busy || !promptsValid}><Check size={13} /> Queue prompt draft</button>
                <button className="btn btn-primary" type="button" onClick={() => void queueEpisode(true)} disabled={busy || !promptsValid || !videoPath.trim()}><Clapperboard size={14} /> Render &amp; queue</button>
              </div>
            </div>
            {result && <div className="tile border-brass text-sm">{result}</div>}
            {copied && <span className="text-[11px] text-brass">Copied {copied}.</span>}
          </div>
        </section>
      </>}
    </div>
  );
}
