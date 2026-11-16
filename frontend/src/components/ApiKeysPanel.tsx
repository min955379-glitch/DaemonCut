"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, ExternalLink, KeyRound, LockKeyhole, RefreshCw, Save, ShieldCheck, X } from "lucide-react";
import { apiGet, apiPut, platformStatusList, type PlatformStatus, type PlatformStatusPayload } from "@/lib/api";

type Props = { onClose: () => void; onToast: (message: string, tone?: "brass" | "alert") => void };
type EnvValue = { configured: boolean; secret: boolean };
type EnvSummary = { values: Record<string, EnvValue>; editable_keys: string[] };
type PlatformEnvelope = { platforms: PlatformStatusPayload };

const FIELDS: Array<{ key: string; label: string; group: string; secret?: boolean; placeholder?: string }> = [
  { key: "GEMINI_API_KEY", label: "Gemini API key", group: "AI generation", secret: true, placeholder: "Paste a new key to replace the saved value" },
  { key: "GEMINI_MODEL_ID", label: "Gemini model ID", group: "AI generation", placeholder: "gemini-4-hydrogen" },
  { key: "YOUTUBE_OAUTH_CLIENT_ID", label: "YouTube OAuth client ID", group: "YouTube", placeholder: "Google Cloud OAuth client ID" },
  { key: "YOUTUBE_OAUTH_CLIENT_SECRET", label: "YouTube OAuth client secret", group: "YouTube", secret: true, placeholder: "Paste a new secret to replace the saved value" },
  { key: "TIKTOK_CLIENT_KEY", label: "TikTok client key", group: "TikTok", placeholder: "TikTok developer client key" },
  { key: "TIKTOK_CLIENT_SECRET", label: "TikTok client secret", group: "TikTok", secret: true, placeholder: "Paste a new secret to replace the saved value" },
  { key: "META_APP_ID", label: "Meta app ID", group: "Meta (optional)", placeholder: "Facebook / Instagram app ID" },
  { key: "META_APP_SECRET", label: "Meta app secret", group: "Meta (optional)", secret: true, placeholder: "Paste a new secret to replace the saved value" },
  { key: "META_PAGE_ID", label: "Meta Facebook Page ID", group: "Meta (optional)", placeholder: "Page ID" },
  { key: "META_INSTAGRAM_USER_ID", label: "Instagram professional account ID", group: "Meta (optional)", placeholder: "Instagram user ID" },
  { key: "META_PAGE_ACCESS_TOKEN", label: "Meta Page access token", group: "Meta (optional)", secret: true, placeholder: "Paste a new token to replace the saved value" }
];

const PLATFORM_LABEL: Record<string, string> = { youtube: "YouTube Shorts", tiktok: "TikTok", facebook: "Facebook Reels", instagram: "Instagram Reels" };

export default function ApiKeysPanel({ onClose, onToast }: Props) {
  const [summary, setSummary] = useState<EnvSummary | null>(null);
  const [platforms, setPlatforms] = useState<PlatformStatus[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({ GEMINI_MODEL_ID: "gemini-4-hydrogen" });
  const [clearKeys, setClearKeys] = useState<string[]>([]);
  const [metaEnabled, setMetaEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [savedKeys, setSavedKeys] = useState<string[]>([]);

  const load = useCallback(async () => {
    setError("");
    try {
      const [settings, status] = await Promise.all([
        apiGet<EnvSummary>("/api/settings/env"),
        apiGet<PlatformEnvelope>("/api/publish/status")
      ]);
      setSummary(settings);
      const statusItems = platformStatusList(status.platforms);
      setPlatforms(statusItems);
      setMetaEnabled(statusItems.some((item) => (item.platform === "facebook" || item.platform === "instagram") && item.configured));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Local key-vault status could not be loaded.");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const refreshOnFocus = () => { void load(); };
    window.addEventListener("focus", refreshOnFocus);
    return () => window.removeEventListener("focus", refreshOnFocus);
  }, [load]);

  async function save() {
    const updates: Record<string, string> = {};
    for (const [key, value] of Object.entries(drafts)) {
      if (value.trim()) updates[key] = value.trim();
    }
    for (const key of clearKeys) updates[key] = "";
    if (metaEnabled) updates.META_API_ENABLED = "true";
    if (!metaEnabled && summary?.values.META_API_ENABLED?.configured) updates.META_API_ENABLED = "false";
    if (!Object.keys(updates).length) { onToast("Enter a new value or choose a field to clear.", "alert"); return; }
    setBusy(true);
    setError("");
    try {
      const result = await apiPut<{ updated_keys: string[]; values: Record<string, EnvValue> }>("/api/settings/env", { updates });
      setSummary((previous) => previous ? { ...previous, values: result.values } : previous);
      setSavedKeys(result.updated_keys ?? Object.keys(updates));
      setDrafts({});
      setClearKeys([]);
      onToast("Local settings were saved. Secret values remain hidden.");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Settings could not be saved.");
    } finally { setBusy(false); }
  }

  async function connect(platform: string) {
    setBusy(true);
    setError("");
    try {
      const result = await apiGet<{ authorization_url: string }>(`/api/oauth/${encodeURIComponent(platform)}/authorize`);
      const popup = window.open(result.authorization_url, "_blank", "popup,width=620,height=760");
      if (popup) {
        popup.opener = null;
        onToast(`Complete ${PLATFORM_LABEL[platform] ?? platform} authorization in the new window.`);
      } else {
        onToast("Allow pop-ups for the local console, then retry OAuth authorization.", "alert");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not start ${platform} authorization.`);
    } finally { setBusy(false); }
  }

  const grouped = FIELDS.reduce<Record<string, typeof FIELDS>>((result, field) => {
    (result[field.group] ??= []).push(field);
    return result;
  }, {});

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="vault-title">
        <div className="panel-head"><div><p className="eyebrow">Local-only settings</p><h2 id="vault-title" className="mt-1 flex items-center gap-2 text-lg font-semibold"><KeyRound size={16} className="text-brass" /> API Key Vault</h2></div><button className="btn btn-quiet" type="button" onClick={onClose} aria-label="Close settings"><X size={16} /></button></div>
        <div className="panel-body space-y-4">
          <div className="tile flex gap-3 border-steel"><LockKeyhole size={18} className="mt-0.5 shrink-0 text-steel" /><p className="text-xs text-parchment/65">Saved secrets are never returned to the browser. Fields stay blank until you enter a replacement. Updates are written to the local <span className="mono">.env</span> file; OAuth tokens are stored separately in the owner-only <span className="mono">.daemoncut/</span> vault.</p></div>
          {error && <p role="alert" className="border-l-2 border-crimson bg-crimson/10 px-3 py-2 text-xs text-crimson">{error}</p>}
          {savedKeys.length > 0 && <p className="flex items-center gap-2 text-xs text-brass"><Check size={14} /> Saved: {savedKeys.join(", ")}</p>}

          <div className="grid gap-3 md:grid-cols-2">
            {Object.entries(grouped).map(([group, fields]) => <section key={group} className="tile space-y-3">
              <div className="flex items-center justify-between"><h3 className="micro-label">{group}</h3><span className="tag tag-steel">{fields.filter((item) => summary?.values[item.key]?.configured).length} configured</span></div>
              {fields.map((field) => {
                const configured = Boolean(summary?.values[field.key]?.configured);
                const clearing = clearKeys.includes(field.key);
                return <label key={field.key} className="field-label">
                  <span className="flex items-center justify-between gap-2"><span>{field.label}</span><span className={`tag ${configured ? "tag-brass" : "tag-alert"}`}>{configured ? "SAVED" : "NOT SET"}</span></span>
                  <span className="flex gap-2"><input className="field" type={field.secret ? "password" : "text"} autoComplete="off" value={drafts[field.key] ?? ""} onChange={(event) => setDrafts((current) => ({ ...current, [field.key]: event.target.value }))} placeholder={configured ? "••••••••  (saved; hidden)" : field.placeholder} aria-label={field.label} /><button className={`btn min-h-0 px-2 py-1 ${clearing ? "btn-danger" : "btn-quiet"}`} type="button" disabled={!configured} onClick={() => setClearKeys((current) => clearing ? current.filter((key) => key !== field.key) : [...current, field.key])} title={configured ? "Toggle clearing this saved value" : "Not configured"}>Clear</button></span>
                </label>;
              })}
            </section>)}
          </div>

          <section className="tile space-y-3">
            <div className="flex items-center justify-between"><div><p className="micro-label">Independent publishing adapters</p><h3 className="mt-1 font-semibold">OAuth account status</h3></div><button className="btn min-h-8 px-2" type="button" onClick={() => void load()} disabled={busy}><RefreshCw size={12} /> Refresh</button></div>
            <div className="grid gap-2 sm:grid-cols-2">
              {platforms.map((platform) => <article key={platform.platform} className="tile flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0"><p className="text-xs font-semibold">{PLATFORM_LABEL[platform.platform] ?? platform.platform}</p><p className="mt-1 text-[10px] text-parchment/65">{platform.message}</p></div>
                <div className="flex items-center gap-2"><span className={`tag ${platform.connected ? "tag-brass" : platform.fallback_available ? "tag-steel" : "tag-alert"}`}>{platform.connected ? "CONNECTED" : platform.fallback_available ? "DRIVE FALLBACK" : platform.configured ? "READY" : "NOT CONFIGURED"}</span>{platform.can_authorize && <button className="btn min-h-8 px-2" type="button" onClick={() => void connect(platform.platform)} disabled={busy}><ExternalLink size={12} /> Connect</button>}</div>
              </article>)}
            </div>
            <label className="flex items-center gap-3 text-xs"><input type="checkbox" checked={metaEnabled} onChange={(event) => setMetaEnabled(event.target.checked)} /><span><span className="block font-semibold">Enable Meta API publishing</span><span className="text-[10px] text-parchment/65">When disabled or unconfigured, Facebook and Instagram use ready-to-post Drive bundles. YouTube and TikTok are unaffected.</span></span></label>
          </section>

          <div className="flex flex-wrap items-center justify-between gap-3"><p className="flex items-center gap-2 text-[10px] text-parchment/65"><ShieldCheck size={14} className="text-brass" /> Only keys typed in this session are sent on Save.</p><div className="flex gap-2"><button className="btn" type="button" onClick={onClose}>Cancel</button><button className="btn btn-primary" type="button" onClick={() => void save()} disabled={busy}><Save size={14} /> Save local settings</button></div></div>
        </div>
      </section>
    </div>
  );
}
