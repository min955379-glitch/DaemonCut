"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, TerminalSquare, Wifi, WifiOff } from "lucide-react";
import { logsSocketUrl, safeStatusColor, type LogEvent } from "@/lib/api";

type ConnectionState = "connecting" | "connected" | "disconnected";

export default function TerminalDrawer() {
  const [expanded, setExpanded] = useState(false);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("disconnected");

  useEffect(() => {
    if (!expanded) return;
    let socket: WebSocket | null = null;
    let heartbeat: number | undefined;
    let alive = true;
    const url = logsSocketUrl();
    if (!url) return;
    setConnection("connecting");
    try {
      socket = new WebSocket(url);
      socket.onopen = () => {
        if (!alive || !socket) return;
        setConnection("connected");
        heartbeat = window.setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send("heartbeat"); }, 20_000);
      };
      socket.onmessage = (message) => {
        if (!alive || typeof message.data !== "string") return;
        try {
          const event = JSON.parse(message.data) as LogEvent;
          if (typeof event.message !== "string" || typeof event.timestamp !== "string") return;
          setEvents((current) => [...current.slice(-199), { ...event, color: safeStatusColor(event.color) }]);
        } catch { /* Ignore malformed log frames. */ }
      };
      socket.onerror = () => { if (alive) setConnection("disconnected"); };
      socket.onclose = () => { if (alive) setConnection("disconnected"); };
    } catch { setConnection("disconnected"); }
    return () => {
      alive = false;
      if (heartbeat) window.clearInterval(heartbeat);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "Terminal closed");
    };
  }, [expanded]);

  return (
    <section className="terminal" aria-label="Live DaemonCut terminal">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
        <div className="flex items-center gap-2"><TerminalSquare size={15} className="text-brass" /><span className="micro-label">DAEMONCUT / LIVE SYSTEM LOG</span><span className={`tag ${connection === "connected" ? "tag-brass" : connection === "connecting" ? "tag-steel" : "tag-alert"}`}>{connection === "connected" ? <Wifi size={10} /> : <WifiOff size={10} />}{connection.toUpperCase()}</span></div>
        <button className="btn btn-quiet min-h-7 px-2 py-1" type="button" aria-expanded={expanded} onClick={() => setExpanded((current) => !current)}>{expanded ? <><ChevronDown size={13} /> Collapse</> : <><ChevronUp size={13} /> Open terminal</>}</button>
      </div>
      {expanded && <div className="terminal-body" role="log" aria-live="polite" aria-relevant="additions text">
        {!events.length && <p className="text-parchment/65">{connection === "connected" ? "Connected. Waiting for API events…" : "Connecting to the local WebSocket log hub…"}</p>}
        {events.map((event, index) => <div className="terminal-line" key={`${event.timestamp}-${index}`}>
          <time className="text-parchment/65" dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleTimeString()}</time>
          <span className="uppercase" style={{ color: safeStatusColor(event.color) }}>{event.level}</span>
          <span className="terminal-message" style={{ color: safeStatusColor(event.color) }}>{event.message}</span>
        </div>)}
      </div>}
    </section>
  );
}
