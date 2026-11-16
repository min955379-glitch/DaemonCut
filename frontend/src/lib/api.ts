/**
 * DaemonCut console API client.
 *
 * All requests are same-origin: Next.js dev/production rewrites proxy
 * `/api/*`, `/bridge*` and `/ws/*` to the local FastAPI service (see
 * next.config.mjs), so the browser never needs to know the backend address.
 */

/** Hard YouTube Create scene-prompt ceiling enforced by the backend. */
export const MAX_YT_CREATE_CHARS = 880;

/** Mirrors backend.config.UI_PALETTE — the only colors the console may use. */
const UI_PALETTE: Record<string, string> = {
  matte_obsidian: "#121316",
  elevated_slate: "#1A1D24",
  structural_border: "#2B303B",
  warm_parchment: "#F4EFE6",
  muted_brass: "#D6AD55",
  deep_steel_blue: "#2B5B84",
  deep_navy: "#102B4E",
  crimson: "#C94A4A",
};

export type Channel = {
  channel_id: string;
  workspace_id?: string;
  platform: string;
  display_name: string;
  external_channel_id: string;
};

export type Episode = {
  episode_id: string;
  workspace_id?: string;
  title: string;
  status: string;
  scheduled_for?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type PlatformStatus = {
  platform: string;
  status: string;
  configured: boolean;
  connected: boolean;
  can_authorize: boolean;
  account_id: string | null;
  fallback_available: boolean;
  message?: string;
};

export type PlatformStatusPayload = {
  platforms: Record<string, PlatformStatus>;
};

/** Canonical platform order used when flattening the status map. */
const PLATFORM_ORDER = ["youtube", "tiktok", "facebook", "instagram"] as const;

export function platformStatusList(payload: PlatformStatusPayload | undefined): PlatformStatus[] {
  if (!payload?.platforms) return [];
  const platforms = payload.platforms;
  const known = PLATFORM_ORDER.filter((name) => platforms[name]);
  const extras = Object.keys(platforms).filter((name) => !PLATFORM_ORDER.includes(name as (typeof PLATFORM_ORDER)[number]));
  return [...known, ...extras.sort()].map((name) => platforms[name]);
}

export type Workspace = {
  workspace_id: string;
  name: string;
  slug: string;
  channels?: Channel[];
};

export type PromptScene = {
  scene_number: number;
  duration_seconds?: number;
  narration?: string;
  visual_description?: string;
  video_prompt: string;
  reference_prompt?: string;
};

export type ScriptDraft = {
  title: string;
  hook?: string;
  script?: string;
  summary?: string;
  tags?: string[];
  reference_prompts?: Record<string, string>;
  scenes: PromptScene[];
};

export type LogEvent = {
  level: string;
  message: string;
  color?: string;
  timestamp: string;
};

function extractMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message: unknown }).message;
      if (typeof message === "string") return message;
    }
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit, fallback: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
  } catch (reason) {
    throw new Error(`DaemonCut API is unreachable at ${path}. Start the FastAPI backend, then retry.`);
  }
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    throw new Error(extractMessage(payload, `Request ${path} failed with ${response.status}.`));
  }
  return payload as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET", cache: "no-store" }, `GET ${path} failed.`);
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }, `POST ${path} failed.`);
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }, `PUT ${path} failed.`);
}

/** WebSocket URL for the live log terminal, relative to the console origin. */
export function logsSocketUrl(): string {
  if (typeof window === "undefined") return "ws://127.0.0.1:8000/ws/logs";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/logs`;
}

/** Resolve a backend status color to a palette color; unknown colors fall back to steel. */
export function safeStatusColor(color: string | undefined | null): string {
  if (!color) return UI_PALETTE.deep_steel_blue;
  if (Object.values(UI_PALETTE).includes(color)) return color;
  return UI_PALETTE.deep_steel_blue;
}
