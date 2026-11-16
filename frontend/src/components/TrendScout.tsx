"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowUpRight, ExternalLink, Flame, RefreshCw, Sparkles } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";

type Props = { workspaceId: string };
type Trend = { topic: string; trend_score?: number; country?: string; approx_traffic?: string; estimated_traffic?: number; repeated_recently?: boolean; source_url?: string; context?: string; cross_country_count?: number };
type TrendEnvelope = { items: Trend[]; source_errors?: string[] };
type Niche = { id: string; name: string; workspace_id?: string; topic?: string; locale?: string; category?: string };
type NicheEnvelope = { niches: Niche[]; subscribed_ids?: string[] };

export default function TrendScout({ workspaceId }: Props) {
  const [trends, setTrends] = useState<Trend[]>([]);
  const [niches, setNiches] = useState<Niche[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refreshedAt, setRefreshedAt] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
      const [trendResult, nicheResult] = await Promise.all([
        apiGet<TrendEnvelope>(`/api/trends${query}`),
        apiGet<NicheEnvelope>(`/api/niches${query}`)
      ]);
      setTrends(trendResult.items ?? []);
      setRefreshedAt(new Date().toISOString());
      setNiches(nicheResult.niches ?? []);
      setSelected(nicheResult.subscribed_ids ?? []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Trend services could not be reached.");
    }
  }, [workspaceId]);

  useEffect(() => { void load(); }, [load]);

  async function refresh() {
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({ force_refresh: "true", limit: "24" });
      if (workspaceId) query.set("workspace_id", workspaceId);
      const result = await apiGet<TrendEnvelope>(`/api/trends?${query.toString()}`);
      setTrends(result.items ?? []);
      setRefreshedAt(new Date().toISOString());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Trend refresh failed.");
    } finally { setBusy(false); }
  }

  async function saveNiches() {
    if (!workspaceId) { setError("Select a workspace before saving niche subscriptions."); return; }
    setBusy(true);
    try {
      await apiPost("/api/niches/subscriptions", { workspace_id: workspaceId, niche_ids: selected });
      setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Niche subscriptions could not be saved."); }
    finally { setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">05 / Niche radar</p><h2 className="mt-1 text-lg font-semibold">Trend Scout</h2></div><button className="btn btn-primary" type="button" onClick={refresh} disabled={busy}><RefreshCw size={13} className={busy ? "animate-spin" : ""} /> Refresh feed</button></div>
        <div className="panel-body space-y-3">
          {error && <p role="alert" className="border-l-2 border-crimson bg-crimson/10 px-3 py-2 text-xs text-crimson">{error}</p>}
          <div className="flex items-center justify-between"><span className="micro-label">RANKED TOPICS · 7-DAY WINDOW</span><span className="text-[10px] text-parchment/65">{refreshedAt ? `UPDATED ${new Date(refreshedAt).toLocaleString()}` : "AWAITING FEED"}</span></div>
          {!trends.length && <div className="empty-state">Trend providers are optional. Configure a source or enter topics directly in a factory.</div>}
          <div className="grid gap-2">
            {trends.slice(0, 24).map((trend, index) => (
              <article key={`${trend.topic}-${index}`} className="tile grid gap-3 sm:grid-cols-[auto_1fr_auto] sm:items-center">
                <span className="rank-mark">{String(index + 1).padStart(2, "0")}</span>
                <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h3 className="font-medium">{trend.topic}</h3>{trend.repeated_recently && <span className="tag tag-alert">Repeated recently</span>}<span className="tag tag-brass"><Flame size={11} /> {trend.trend_score ?? "—"}</span></div><p className="mt-1 text-xs text-parchment/65">{trend.context ?? `${trend.country ?? "Global"} · ${trend.approx_traffic || "traffic n/a"}${trend.cross_country_count ? ` · ${trend.cross_country_count} markets` : ""}`}</p><p className="mt-1 micro-label">{trend.estimated_traffic ? `${trend.estimated_traffic.toLocaleString()} EST. SEARCHES` : "TREND FEED"}</p></div>
                {trend.source_url && <a className="btn min-h-8 px-2" href={trend.source_url} target="_blank" rel="noreferrer" aria-label={`Open source for ${trend.topic}`}><ExternalLink size={12} /></a>}
              </article>
            ))}
          </div>
          <p className="text-[11px] text-parchment/65"><Sparkles size={12} className="mr-1 inline text-brass" />A trend is a research lead, not a verified fact. Confirm the source before scripting.</p>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Niche subscriptions</p><h3 className="mt-1 font-semibold">Control the signal</h3></div></div>
        <div className="panel-body space-y-3">
          {!niches.length && <p className="text-xs text-parchment/65">No saved niches yet. Create one in Universal Niche Builder.</p>}
          <div className="grid gap-2 sm:grid-cols-2">
            {niches.map((niche) => <label key={niche.id} className="tile flex items-center gap-3"><input type="checkbox" checked={selected.includes(niche.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, niche.id] : current.filter((id) => id !== niche.id))} /><span className="flex-1 text-sm">{niche.name}</span><span className="text-[10px] text-parchment/65">{niche.locale ?? niche.category ?? "LOCAL"}</span></label>)}
          </div>
          {!!niches.length && <div className="flex justify-end"><button className="btn" type="button" onClick={saveNiches} disabled={busy}>Save subscriptions <ArrowUpRight size={13} /></button></div>}
          <p className="text-[11px] text-parchment/65">Subscriptions affect suggestions; they do not publish anything automatically.</p>
        </div>
      </section>
    </div>
  );
}
