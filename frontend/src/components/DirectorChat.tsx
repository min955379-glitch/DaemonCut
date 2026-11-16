"use client";

import { useEffect, useRef, useState } from "react";
import { Check, CirclePause, CirclePlay, Globe2, Mic, Send, Sparkles, Upload } from "lucide-react";
import { apiPost, MAX_YT_CREATE_CHARS, type PromptScene } from "@/lib/api";

type Props = { onToast: (message: string, tone?: "brass" | "alert") => void };
type SpeechResult = { isFinal: boolean; 0: { transcript: string } };
type SpeechResults = ArrayLike<SpeechResult>;
type SpeechEvent = Event & { resultIndex: number; results: SpeechResults };
type SpeechErrorEvent = Event & { error: string };
type Recognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechEvent) => void) | null;
  onerror: ((event: SpeechErrorEvent) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
};
type SpeechWindow = Window & { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
type ChatResult = { response: { reply?: string; tool?: string | null; success?: boolean; result?: unknown } };
type InspectResult = { source: Record<string, unknown> };
type CloneResult = { clone: Record<string, unknown> };
type TranscriptionResult = { transcription: { text?: string; transcript?: string; [key: string]: unknown } };

export default function DirectorChat({ onToast }: Props) {
  const [sessionId, setSessionId] = useState("");
  const [transcript, setTranscript] = useState("");
  const [paused, setPaused] = useState(false);
  const [recording, setRecording] = useState(false);
  const [chatText, setChatText] = useState("");
  const [messages, setMessages] = useState<Array<{ role: "director" | "copilot"; text: string; scenes?: PromptScene[] }>>([]);
  const [busy, setBusy] = useState(false);
  const [inspectUrl, setInspectUrl] = useState("");
  const [inspectResult, setInspectResult] = useState<Record<string, unknown> | null>(null);
  const [cloneResult, setCloneResult] = useState<Record<string, unknown> | null>(null);
  const recognitionRef = useRef<Recognition | null>(null);
  const shouldContinueRef = useRef(false);

  useEffect(() => {
    const stored = sessionStorage.getItem("daemoncut-dictation-session") || crypto.randomUUID();
    sessionStorage.setItem("daemoncut-dictation-session", stored);
    setSessionId(stored);
    let alive = true;
    apiPost<{ draft?: { text?: string; paused?: boolean } }>(`/api/director/dictation/${encodeURIComponent(stored)}/resume`)
      .then((value) => { if (alive && value.draft) { setTranscript(value.draft.text ?? ""); setPaused(Boolean(value.draft.paused)); } })
      .catch(() => undefined);
    return () => { alive = false; recognitionRef.current?.abort(); };
  }, []);

  function appendTranscript(text: string) {
    const chunk = text.trim();
    if (!chunk) return;
    setTranscript((current) => `${current}${current ? " " : ""}${chunk}`);
    if (sessionId) {
      void apiPost(`/api/director/dictation/${encodeURIComponent(sessionId)}/chunk`, { text: chunk })
        .catch((error: unknown) => onToast(error instanceof Error ? error.message : "Dictation draft could not be saved.", "alert"));
    }
  }

  function updateScenePrompt(messageIndex: number, sceneIndex: number, value: string) {
    setMessages((current) => current.map((message, index) => index === messageIndex && message.scenes
      ? { ...message, scenes: message.scenes.map((scene, currentScene) => currentScene === sceneIndex ? { ...scene, video_prompt: value.slice(0, MAX_YT_CREATE_CHARS) } : scene) }
      : message));
  }

  function startRecognition() {
    const speechWindow = window as SpeechWindow;
    const RecognitionConstructor = speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition;
    if (!RecognitionConstructor) {
      onToast("This browser has no Web Speech API. Upload a short audio chunk for local Whisper instead.", "alert");
      return;
    }
    try {
      const recognition = new RecognitionConstructor();
      recognition.continuous = true;
      recognition.interimResults = false;
      recognition.lang = "en-US";
      recognition.onresult = (event) => {
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const result = event.results[index];
          if (result?.isFinal) appendTranscript(result[0]?.transcript ?? "");
        }
      };
      recognition.onerror = (event) => onToast(`Microphone capture stopped: ${event.error}. Your saved words remain in the draft.`, "alert");
      recognition.onend = () => {
        if (shouldContinueRef.current) {
          try { recognition.start(); } catch { setRecording(false); }
        } else {
          setRecording(false);
        }
      };
      recognitionRef.current = recognition;
      shouldContinueRef.current = true;
      recognition.start();
      setRecording(true);
      setPaused(false);
    } catch {
      setRecording(false);
      onToast("Microphone permission was unavailable.", "alert");
    }
  }

  async function pauseRecording() {
    shouldContinueRef.current = false;
    recognitionRef.current?.stop();
    setRecording(false);
    if (sessionId) {
      try {
        const result = await apiPost<{ draft?: { text?: string } }>(`/api/director/dictation/${encodeURIComponent(sessionId)}/pause`);
        setTranscript(result.draft?.text ?? transcript);
      } catch (error) { onToast(error instanceof Error ? error.message : "Could not pause the saved draft.", "alert"); }
    }
    setPaused(true);
  }

  async function resumeRecording() {
    if (sessionId) {
      try {
        const result = await apiPost<{ draft?: { text?: string } }>(`/api/director/dictation/${encodeURIComponent(sessionId)}/resume`);
        setTranscript(result.draft?.text ?? transcript);
      } catch (error) { onToast(error instanceof Error ? error.message : "Could not resume the saved draft.", "alert"); return; }
    }
    startRecognition();
  }

  async function finalizeDraft() {
    if (!sessionId) return;
    try {
      const response = await apiPost<{ text: string }>(`/api/director/dictation/${encodeURIComponent(sessionId)}/finalize`);
      setTranscript(response.text);
      setChatText((current) => current ? `${current}\n${response.text}` : response.text);
      setPaused(false);
      onToast("Dictation draft finalized and copied into the director prompt.");
    } catch (error) { onToast(error instanceof Error ? error.message : "Draft could not be finalized.", "alert"); }
  }

  async function sendMessage() {
    const message = chatText.trim();
    if (!message) return;
    setMessages((current) => [...current, { role: "director", text: message }]);
    setChatText("");
    setBusy(true);
    try {
      const response = await apiPost<ChatResult>("/api/director/chat", { message, context: { transcript, current_url: inspectUrl } });
      const scenes = extractScenePrompts(response.response.result);
      setMessages((current) => [...current, { role: "copilot", text: response.response.reply ?? "The studio action completed.", ...(scenes.length ? { scenes } : {}) }]);
    } catch (error) {
      setMessages((current) => [...current, { role: "copilot", text: error instanceof Error ? error.message : "The director request failed." }]);
    } finally { setBusy(false); }
  }

  async function inspectSource(clone: boolean) {
    if (!inspectUrl.trim()) { onToast("Paste a public video link first.", "alert"); return; }
    setBusy(true);
    try {
      if (clone) {
        const value = await apiPost<CloneResult>("/api/browser/clone", { url: inspectUrl, niche: "original short-form story", inspect_dom: true });
        setCloneResult(value.clone);
      } else {
        const value = await apiPost<InspectResult>("/api/browser/inspect", { url: inspectUrl, inspect_dom: true });
        setInspectResult(value.source);
      }
    } catch (error) { onToast(error instanceof Error ? error.message : "Public-page inspection failed.", "alert"); }
    finally { setBusy(false); }
  }

  async function transcribeFile(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("language", "en");
    setBusy(true);
    try {
      const response = await apiPost<TranscriptionResult>("/api/director/transcribe", form);
      const text = response.transcription.text ?? response.transcription.transcript ?? "";
      appendTranscript(text);
      onToast("Audio transcribed locally; the dictation draft was preserved.");
    } catch (error) { onToast(error instanceof Error ? error.message : "Local transcription failed.", "alert"); }
    finally { setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-3">
      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">04 / Director copilot</p><h2 className="mt-1 text-lg font-semibold">Talk through the next cut</h2></div><span className={`tag ${recording ? "tag-alert" : "tag-brass"}`}><span className={`status-led ${recording ? "status-led-alert" : ""}`} />{recording ? "MIC ACTIVE" : paused ? "DRAFT PAUSED" : "STANDBY"}</span></div>
        <div className="panel-body space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {!recording && !paused && <button className="btn btn-primary" type="button" onClick={startRecognition}><Mic size={14} /> Start dictation</button>}
            {recording && <button className="btn btn-danger" type="button" onClick={() => void pauseRecording()}><CirclePause size={14} /> Pause &amp; keep draft</button>}
            {paused && <button className="btn btn-primary" type="button" onClick={() => void resumeRecording()}><CirclePlay size={14} /> Resume from saved words</button>}
            <button className="btn" type="button" onClick={() => void finalizeDraft()} disabled={!transcript}><Check size={14} /> Finalize words</button>
            <label className="btn cursor-pointer" htmlFor="whisper-file"><Upload size={14} /> Local Whisper<input id="whisper-file" className="sr-only" type="file" accept="audio/wav,audio/mpeg,audio/mp4,audio/flac,audio/ogg,audio/aac,.wav,.mp3,.m4a,.flac,.ogg,.aac" onChange={(event) => void transcribeFile(event.target.files?.[0])} /></label>
            <span className="text-[11px] text-parchment/65">Pause and resume persist the same session; confirmed words are appended, never cleared.</span>
          </div>
          <label className="field-label">Persistent dictation draft
            <textarea className="textarea min-h-32" value={transcript} onChange={(event) => setTranscript(event.target.value)} placeholder="Speech recognition and local transcription append here…" maxLength={100_000} />
          </label>
          <div className="flex items-center justify-between text-[10px] text-parchment/65"><span className="mono">SESSION / {sessionId ? sessionId.slice(0, 12) : "INITIALIZING"}</span><span className="mono">{transcript.length.toLocaleString()} CHARS</span></div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Conversation</p><h3 className="mt-1 font-semibold">Studio tools, one clear request at a time</h3></div><Sparkles size={16} className="text-brass" /></div>
        <div className="panel-body space-y-3">
          <div className="max-h-80 space-y-2 overflow-auto">
            {!messages.length && <div className="empty-state min-h-24">Ask for a script adjustment, subtitle pass, edit, or thumbnail.</div>}
            {messages.map((message, messageIndex) => <div key={`${message.role}-${messageIndex}`} className={`tile ${message.role === "director" ? "border-steel" : "border-brass"}`}>
              <p className="micro-label">{message.role === "director" ? "DIRECTOR" : "COPILOT"}</p>
              <p className="mt-2 whitespace-pre-wrap text-sm">{message.text}</p>
              {message.scenes?.map((scene, sceneIndex) => {
                const value = scene.video_prompt.slice(0, MAX_YT_CREATE_CHARS);
                return <article key={`${scene.scene_number ?? sceneIndex}`} className="prompt-card mt-3">
                  <div className="prompt-card-head"><span className="eyebrow">Scene {scene.scene_number ?? sceneIndex + 1} / YouTube Create</span><span className="flex items-center gap-2"><span className="counter">{value.length} / {MAX_YT_CREATE_CHARS}</span><button className="btn btn-quiet min-h-0 px-2 py-1" type="button" onClick={() => void navigator.clipboard.writeText(value).then(() => onToast("Scene prompt copied.")).catch(() => onToast("Clipboard access was denied by the browser.", "alert"))}>Copy</button></span></div>
                  <textarea className="prompt-text" value={value} maxLength={MAX_YT_CREATE_CHARS} spellCheck={false} onChange={(event) => updateScenePrompt(messageIndex, sceneIndex, event.target.value)} aria-label={`Director scene prompt ${sceneIndex + 1}`} />
                </article>;
              })}
            </div>)}
          </div>
          <div className="flex gap-2"><textarea className="textarea min-h-[60px] flex-1" value={chatText} onChange={(event) => setChatText(event.target.value)} maxLength={20_000} placeholder="e.g. tighten the hook, keep the last shot gentle…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendMessage(); } }} /><button className="btn btn-primary self-end" type="button" onClick={sendMessage} disabled={busy || !chatText.trim()}><Send size={14} /> Send</button></div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><div><p className="eyebrow">Mini browser inspector</p><h3 className="mt-1 font-semibold">Inspect public structure, write an original story</h3></div><Globe2 size={16} className="text-steel" /></div>
        <div className="panel-body space-y-3">
          <div className="flex flex-col gap-2 sm:flex-row"><input className="field flex-1" value={inspectUrl} onChange={(event) => setInspectUrl(event.target.value)} placeholder="https://www.youtube.com/shorts/…" /><button className="btn" type="button" onClick={() => void inspectSource(false)} disabled={busy}><Globe2 size={13} /> Inspect</button><button className="btn btn-primary" type="button" onClick={() => void inspectSource(true)} disabled={busy}><Sparkles size={13} /> Adapt structure</button></div>
          {(inspectResult || cloneResult) && <pre className="code-block max-h-60">{JSON.stringify(cloneResult ?? inspectResult, null, 2)}</pre>}
          <p className="text-[11px] text-parchment/65">The adapter receives high-level pacing and beat metadata; it does not reproduce source dialogue or distinctive scenes.</p>
        </div>
      </section>
    </div>
  );
}

function extractScenePrompts(payload: unknown): PromptScene[] {
  if (typeof payload !== "object" || payload === null) return [];
  const root = payload as Record<string, unknown>;
  const nested = typeof root.script === "object" && root.script !== null ? root.script as Record<string, unknown> : root;
  if (!Array.isArray(nested.scenes)) return [];
  return nested.scenes.flatMap((scene, index) => {
    if (typeof scene !== "object" || scene === null) return [];
    const item = scene as Record<string, unknown>;
    if (typeof item.video_prompt !== "string") return [];
    return [{ ...item, scene_number: typeof item.scene_number === "number" ? item.scene_number : index + 1, video_prompt: item.video_prompt.slice(0, MAX_YT_CREATE_CHARS) } as PromptScene];
  });
}
