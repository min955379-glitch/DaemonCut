"use client";

import { useState } from "react";
import { BookOpenCheck, LoaderCircle, Sparkles } from "lucide-react";
import { apiPost } from "@/lib/api";
import ScriptPromptWorkspace from "./ScriptPromptWorkspace";

type Props = { workspaceId: string; onToast: (message: string, tone?: "brass" | "alert") => void; onSaved?: () => void };
type Blueprint = Record<string, unknown>;
type SeriesBible = { name: string; audience: string; promise: string; tone: string; boundaries: string[]; contentPillars: unknown; visualCanon: string; blueprint: Blueprint };

export default function NicheStudio({ workspaceId, onToast, onSaved }: Props) {
  const [niche, setNiche] = useState("");
  const [audience, setAudience] = useState("Curious viewers who want concise, accurate short-form stories");
  const [promise, setPromise] = useState("");
  const [tone, setTone] = useState("Grounded, original, respectful, and easy to follow");
  const [boundaries, setBoundaries] = useState("");
  const [blueprint, setBlueprint] = useState<Blueprint | null>(null);
  const [seriesBible, setSeriesBible] = useState<SeriesBible | null>(null);
  const [busy, setBusy] = useState(false);

  async function buildStrategy() {
    if (!niche.trim() || !promise.trim()) { onToast("Add a niche and channel promise first.", "alert"); return; }
    setBusy(true);
    try {
      const result = await apiPost<{ blueprint: Blueprint }>("/api/studio/strategy", {
        niche,
        target_audience: audience,
        channel_promise: promise,
        platforms: ["YouTube Shorts", "TikTok", "Instagram Reels"],
        tone,
        content_boundaries: boundaries.split("\n").map((item) => item.trim()).filter(Boolean).slice(0, 20),
        preferred_languages: ["English"]
      });
      setBlueprint(result.blueprint);
      const bible: SeriesBible = {
        name: niche.trim(),
        audience: audience.trim(),
        promise: promise.trim(),
        tone: tone.trim(),
        boundaries: boundaries.split("\n").map((item) => item.trim()).filter(Boolean),
        contentPillars: result.blueprint.content_pillars ?? [],
        visualCanon: "Consistent original visual language; use the same palette, silhouette rules, wardrobe continuity, and environment logic across episodes.",
        blueprint: result.blueprint
      };
      setSeriesBible(bible);
      if (workspaceId) {
        await apiPost("/api/niches", {
          workspace_id: workspaceId,
          name: niche.trim(),
          target_audience: audience.trim(),
          channel_promise: promise.trim(),
          tone: tone.trim(),
          boundaries: bible.boundaries,
          blueprint: result.blueprint,
          series_bible: bible
        });
      }
      onSaved?.();
      onToast(workspaceId ? "Master strategy and Series Bible saved to this workspace." : "Master strategy and Series Bible created.");
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Strategy generation failed.", "alert");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">03 / Universal niche builder</p><h2 className="mt-1 text-lg font-semibold">Add new niche</h2></div><span className="tag tag-steel"><Sparkles size={12} /> Strategy blueprint</span></div>
        <div className="panel-body grid gap-3 md:grid-cols-2">
          <label className="field-label">Custom genre or niche
            <input className="field" value={niche} onChange={(event) => setNiche(event.target.value)} placeholder="True crime, Stoic philosophy, practical history…" maxLength={160} />
          </label>
          <label className="field-label">Target audience
            <input className="field" value={audience} onChange={(event) => setAudience(event.target.value)} maxLength={200} />
          </label>
          <label className="field-label md:col-span-2">Channel promise
            <textarea className="textarea" value={promise} onChange={(event) => setPromise(event.target.value)} placeholder="What can viewers reliably expect from each episode?" maxLength={500} />
          </label>
          <label className="field-label">Tone and style
            <input className="field" value={tone} onChange={(event) => setTone(event.target.value)} maxLength={300} />
          </label>
          <label className="field-label">Accuracy / content boundaries, one per line
            <textarea className="textarea" value={boundaries} onChange={(event) => setBoundaries(event.target.value)} placeholder="Separate verified facts from hypotheses\nAvoid identifying private individuals" maxLength={2_000} />
          </label>
          <div className="md:col-span-2 flex justify-end">
            <button className="btn btn-primary" type="button" disabled={busy} onClick={buildStrategy}><BookOpenCheck size={14} /> {busy ? <LoaderCircle className="animate-spin" size={14} /> : null} Build master strategy</button>
          </div>
        </div>
      </section>
      {blueprint && seriesBible && <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Master strategy blueprint / series bible</p><h3 className="mt-1 font-semibold">{niche}</h3></div><span className="tag tag-brass">Review before publishing</span></div>
        <div className="panel-body grid gap-3 lg:grid-cols-2">
          <article className="tile space-y-2"><p className="micro-label">SERIES BIBLE</p><p className="text-sm"><strong>Promise:</strong> {seriesBible.promise}</p><p className="text-sm"><strong>Audience:</strong> {seriesBible.audience}</p><p className="text-sm"><strong>Tone:</strong> {seriesBible.tone}</p><p className="text-sm"><strong>Visual canon:</strong> {seriesBible.visualCanon}</p><p className="text-sm"><strong>Content pillars:</strong> {Array.isArray(seriesBible.contentPillars) ? seriesBible.contentPillars.map((pillar) => typeof pillar === "string" ? pillar : JSON.stringify(pillar)).join(" · ") : String(seriesBible.contentPillars)}</p><p className="text-sm"><strong>Boundaries:</strong> {seriesBible.boundaries.join(" · ") || "No additional boundaries supplied"}</p></article>
          <div><p className="micro-label mb-2">STRATEGY BLUEPRINT JSON</p><pre className="code-block max-h-80">{JSON.stringify(blueprint, null, 2)}</pre></div>
        </div>
      </section>}
      {niche.trim() && <ScriptPromptWorkspace
        key={niche}
        workspaceId={workspaceId}
        initialNiche={niche}
        initialStyle={`${tone}. Original details, careful sourcing, clear boundaries, concise visual beats.`}
        seriesKey="custom_universal_niche"
        onToast={onToast}
      />}
    </div>
  );
}
