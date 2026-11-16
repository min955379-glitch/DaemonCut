"use client";

import { useState } from "react";
import { Check, Film, LoaderCircle, Scissors, Sparkles, WandSparkles } from "lucide-react";
import { apiPost } from "@/lib/api";

type Props = { workspaceId: string; onToast: (message: string, tone?: "brass" | "alert") => void };
type Highlight = { start_seconds: number; end_seconds: number; duration_seconds?: number; transcript: string; audio_score: number; hook_score: number; viral_score: number; explanation?: string };
type Render = { highlight?: Highlight; render?: { path?: string; duration_seconds?: number }; subtitles?: { srt_path?: string }; thumbnails?: { selected_path?: string; paths?: string[] } };
type Result = { media?: { path?: string; title?: string; duration_seconds?: number }; highlights: Highlight[]; renders: Render[] };

const FRAME_OPTIONS = [
  { value: "whole_blur", label: "Whole + blur" },
  { value: "zoom_blur", label: "Zoom + blur" },
  { value: "full_screen", label: "Full screen" }
] as const;
const LANGUAGE_OPTIONS = [
  { value: "english", label: "100% English" },
  { value: "roman_urdu", label: "Roman Urdu" },
  { value: "roman_hindi", label: "Roman Hindi" }
] as const;

export default function StreamClipper({ workspaceId, onToast }: Props) {
  const [inputMode, setInputMode] = useState<"url" | "path">("url");
  const [sourceUrl, setSourceUrl] = useState("");
  const [videoPath, setVideoPath] = useState("");
  const [title, setTitle] = useState("");
  const [startSeconds, setStartSeconds] = useState("0");
  const [captureDuration, setCaptureDuration] = useState("180");
  const [maxHighlights, setMaxHighlights] = useState("5");
  const [framingMode, setFramingMode] = useState<(typeof FRAME_OPTIONS)[number]["value"]>("whole_blur");
  const [centerX, setCenterX] = useState(50);
  const [languageMode, setLanguageMode] = useState<(typeof LANGUAGE_OPTIONS)[number]["value"]>("english");
  const [hook, setHook] = useState("");
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [renders, setRenders] = useState<Render[]>([]);
  const [mediaPath, setMediaPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function analyze(renderSelected: boolean) {
    const storedSource = renderSelected ? mediaPath.trim() : "";
    const source = storedSource || (inputMode === "url" ? sourceUrl.trim() : videoPath.trim());
    if (!source) { onToast(inputMode === "url" ? "Paste a public YouTube, Twitch, or Kick VOD URL." : "Enter the local Drive-mapped video path.", "alert"); return; }
    if (renderSelected && !selected.length) { onToast("Select at least one highlight to render.", "alert"); return; }
    setBusy(true);
    setError("");
    if (!renderSelected) setRenders([]);
    try {
      const response = await apiPost<Result>("/api/clipper/highlights", {
        ...(storedSource ? { video_path: storedSource, title: title || undefined } : inputMode === "url" ? { source_url: source } : { video_path: source, title: title || undefined }),
        start_seconds: Number(startSeconds) || 0,
        capture_duration_seconds: Number(captureDuration) || 180,
        workspace_id: workspaceId || undefined,
        window_seconds: 30,
        step_seconds: 8,
        max_highlights: Math.max(1, Math.min(20, Number(maxHighlights) || 5)),
        use_brain: true,
        render: renderSelected,
        selected_highlight_indices: renderSelected ? selected : [],
        selected_highlights: renderSelected ? selected.map((index) => highlights[index]).filter((item): item is Highlight => Boolean(item)) : [],
        framing_mode: framingMode,
        center_x: centerX / 100,
        language_mode: languageMode,
        make_thumbnail: true,
        hook: hook.trim() || undefined
      });
      if (response.highlights) {
        setHighlights(response.highlights);
        if (!renderSelected) setSelected(response.highlights.map((_, index) => index));
      }
      setMediaPath(response.media?.path ?? videoPath);
      if (renderSelected) {
        setRenders(response.renders ?? []);
        onToast(`Rendered ${response.renders?.length ?? 0} selected highlight${response.renders?.length === 1 ? "" : "s"}.`);
      } else {
        onToast(`PeakCut ranked ${response.highlights?.length ?? 0} highlight candidates.`);
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Clipper could not process this source.";
      setError(message);
      onToast(message, "alert");
    } finally { setBusy(false); }
  }

  function toggle(index: number) {
    setSelected((current) => current.includes(index) ? current.filter((item) => item !== index) : [...current, index].sort((a, b) => a - b));
  }

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">06 / PeakCut</p><h2 className="mt-1 text-lg font-semibold">Long-form &amp; stream auto-clipper</h2></div><span className="tag tag-steel"><Scissors size={12} /> 15–60 sec vertical shorts</span></div>
        <div className="panel-body grid gap-3 md:grid-cols-2">
          <div className="md:col-span-2 flex flex-wrap gap-2" role="group" aria-label="Source type"><button className={`btn ${inputMode === "url" ? "btn-primary" : ""}`} type="button" onClick={() => setInputMode("url")}>Public VOD URL</button><button className={`btn ${inputMode === "path" ? "btn-primary" : ""}`} type="button" onClick={() => setInputMode("path")}>Local / Drive path</button></div>
          {inputMode === "url" ? <label className="field-label md:col-span-2">YouTube / Twitch / Kick source URL<input className="field" type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://www.youtube.com/watch?v=…" maxLength={2048} /></label> : <>
            <label className="field-label md:col-span-2">Local clip path, preferably under the DaemonCut Development Drive root<input className="field font-mono" value={videoPath} onChange={(event) => setVideoPath(event.target.value)} placeholder="G:\\My Drive\\DaemonCut Development\\source_media\\session.mp4" /></label>
            <label className="field-label">Clip title<input className="field" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} /></label>
          </>}
          <label className="field-label">Capture start seconds<input className="field" type="number" min="0" value={startSeconds} onChange={(event) => setStartSeconds(event.target.value)} /></label>
          <label className="field-label">Capture duration seconds (15–3600)<input className="field" type="number" min="15" max="3600" value={captureDuration} onChange={(event) => setCaptureDuration(event.target.value)} /></label>
          <label className="field-label">Top highlights to rank<input className="field" type="number" min="1" max="20" value={maxHighlights} onChange={(event) => setMaxHighlights(event.target.value)} /></label>
          <label className="field-label">Vertical framing<select className="select" value={framingMode} onChange={(event) => setFramingMode(event.target.value as typeof framingMode)}>{FRAME_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
          <label className="field-label">Caption language<select className="select" value={languageMode} onChange={(event) => setLanguageMode(event.target.value as typeof languageMode)}>{LANGUAGE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
          <label className="field-label">Frame center X <span className="flex items-center gap-3"><input className="w-full accent-brass" type="range" min="0" max="100" value={centerX} onChange={(event) => setCenterX(Number(event.target.value))} /><span className="mono w-12 text-right text-brass">{centerX}%</span></span></label>
          <label className="field-label">Optional three-word thumbnail hook<input className="field" value={hook} onChange={(event) => { const words = event.target.value.split(/\s+/); setHook(words.length > 3 ? words.slice(0, 3).join(" ") : event.target.value); }} maxLength={80} placeholder="Leave blank for an automatic three-word hook" /><span className="counter">{hook.trim() ? hook.trim().split(/\s+/).length : 0} / 3 WORDS</span></label>
          <div className="md:col-span-2 flex flex-wrap items-center justify-between gap-3"><p className="text-[11px] text-parchment/65">PeakCut rejects unsupported hosts and path escapes. Rendering requires FFmpeg and local GPU/CPU encoders.</p><button className="btn btn-primary" type="button" onClick={() => void analyze(false)} disabled={busy}><WandSparkles size={14} /> {busy ? <LoaderCircle className="animate-spin" size={14} /> : null} Analyze highlights</button></div>
        </div>
      </section>

      {error && <div role="alert" className="panel border-crimson p-3 text-sm text-crimson">{error}</div>}
      {!!highlights.length && <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Ranked candidates</p><h3 className="mt-1 font-semibold">{highlights.length} non-overlapping windows</h3></div><span className="tag tag-brass">{selected.length} selected</span></div>
        <div className="panel-body space-y-3">
          {mediaPath && <p className="text-[10px] text-parchment/65">SOURCE / <span className="mono">{mediaPath}</span></p>}
          <div className="grid gap-2">
            {highlights.map((item, index) => <label key={`${item.start_seconds}-${index}`} className={`tile flex gap-3 ${selected.includes(index) ? "border-brass" : ""}`}>
              <input className="mt-1" type="checkbox" checked={selected.includes(index)} onChange={() => toggle(index)} aria-label={`Select highlight ${index + 1}`} />
              <span className="rank-mark">{String(index + 1).padStart(2, "0")}</span>
              <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2"><strong className="text-sm">{formatTime(item.start_seconds)} – {formatTime(item.end_seconds)}</strong><span className="tag tag-brass">VIRAL {item.viral_score.toFixed(2)}</span><span className="tag tag-steel">AUDIO {item.audio_score.toFixed(2)}</span></span><span className="mt-2 block text-xs">{item.transcript || "No transcript available for this window."}</span><span className="mt-1 block text-[10px] text-parchment/65">{item.explanation || "Audio energy and narrative hook score"}</span></span>
            </label>)}
          </div>
          <div className="flex justify-end"><button className="btn btn-primary" type="button" onClick={() => void analyze(true)} disabled={busy || !selected.length}><Film size={14} /> Render selected + 3-word thumbnails</button></div>
          {!!renders.length && <div className="space-y-2"><p className="micro-label">Rendered media</p>{renders.map((render, index) => <div key={`${render.render?.path ?? index}`} className="tile flex flex-wrap items-center justify-between gap-2"><div><p className="text-xs font-semibold">{render.render?.duration_seconds?.toFixed(1) ?? "—"} sec · {render.render?.path ?? "Render complete"}</p><p className="mt-1 text-[10px] text-parchment/65">{render.subtitles?.srt_path ?? "No subtitle sidecar"}{render.thumbnails?.selected_path ? ` · Thumbnail: ${render.thumbnails.selected_path}` : ""}</p></div><span className="tag tag-brass"><Check size={11} /> READY</span></div>)}</div>}
          <p className="flex items-center gap-2 text-[10px] text-parchment/65"><Sparkles size={12} className="text-brass" />Select separate highlights before rendering. Caption language and framing apply to each selected clip.</p>
        </div>
      </section>}
    </div>
  );
}

function formatTime(value: number) {
  const seconds = Math.max(0, Math.floor(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
