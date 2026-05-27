import { onBeforeUnmount, ref, type Ref } from "vue";

import { useAuthStore } from "@/store/auth";

export type AgentStreamMsg =
  | { t: "hello"; agent: string; server_time: number }
  | { t: "delta"; text: string }
  | { t: "tool_call"; id: string; name: string; args: unknown }
  | { t: "tool_result"; id: string; result: unknown }
  | {
      t: "done";
      tool_calls_made?: number;
      total_tokens?: number;
      source?: string;
      /** Structured rich-message segments. Present when the server
       *  resolved a DSL handler that emitted images / mixed content;
       *  absent for plain-text-only replies (frontend then falls
       *  back to the streamed ``delta`` text). */
      segments?: Array<{
        kind: "text" | "image";
        text?: string;
        url?: string;
        alt?: string;
        delay_before_s?: number;
      }>;
    }
  | { t: "error"; msg: string }
  | { t: "ping" };

export interface UseAgentStreamOptions {
  onMessage?: (msg: AgentStreamMsg) => void;
  /** Auto-reconnect on unexpected close (network drop). Default true. */
  autoReconnect?: boolean;
}

function wsBase(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

export function useAgentStream(agent: string, opts: UseAgentStreamOptions = {}) {
  const auth = useAuthStore();
  const status: Ref<"closed" | "connecting" | "open" | "reconnecting"> = ref("closed");
  let ws: WebSocket | null = null;
  /**
   * Set when caller invokes close() explicitly. Suppresses both auto-reconnect
   * and any in-flight onmessage callbacks (the latter avoids races between
   * "close old stream → open new stream" when switching agents — old ws may
   * still deliver a delta after close handshake).
   */
  let closedByUser = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let backoff = 800;
  /** Resolves on the *next* successful open. Renewed each open() call. */
  let openResolve: (() => void) | null = null;
  let openReject: ((err: Error) => void) | null = null;
  let openPromise: Promise<void> = Promise.reject(new Error("not opened"));
  // Swallow the initial unhandled rejection (no one is awaiting yet).
  openPromise.catch(() => {});

  function renewOpenPromise() {
    openPromise = new Promise<void>((resolve, reject) => {
      openResolve = resolve;
      openReject = reject;
    });
    // Keep an attached catch so if no one awaits, no UnhandledPromiseRejection.
    openPromise.catch(() => {});
  }

  function clearReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function connect() {
    if (!auth.accessToken) return;
    status.value = ws ? "reconnecting" : "connecting";
    const url = `${wsBase()}/ws/agents/${encodeURIComponent(agent)}/stream?token=${encodeURIComponent(auth.accessToken)}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      status.value = "open";
      backoff = 800;
      openResolve?.();
      openResolve = null;
      openReject = null;
    };

    ws.onmessage = (ev) => {
      // Ignore late messages from a connection that's been closed by the user
      // (e.g. switched agent). Without this guard, an in-flight delta from the
      // old socket could land inside the new conversation's bubble.
      if (closedByUser) return;
      try {
        const msg = JSON.parse(ev.data) as AgentStreamMsg;
        opts.onMessage?.(msg);
      } catch {
        // no-op
      }
    };

    ws.onerror = () => {
      // handled via onclose
    };

    ws.onclose = () => {
      ws = null;
      if (closedByUser) {
        status.value = "closed";
        const rej = openReject;
        openResolve = null;
        openReject = null;
        rej?.(new Error("closed"));
        return;
      }
      // Auto-reconnect with exponential backoff. The chat caller still has a
      // valid stream handle; pending input() calls will await whenOpen() and
      // resume after reconnect succeeds.
      if (opts.autoReconnect !== false && auth.isAuthed) {
        status.value = "reconnecting";
        backoff = Math.min(Math.floor(backoff * 1.6), 12_000);
        clearReconnect();
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          if (!closedByUser) connect();
        }, backoff);
      } else {
        status.value = "closed";
        const rej = openReject;
        openResolve = null;
        openReject = null;
        rej?.(new Error("closed"));
      }
    };
  }

  function open() {
    if (ws) return;
    closedByUser = false;
    renewOpenPromise();
    connect();
  }

  function close() {
    closedByUser = true;
    clearReconnect();
    ws?.close();
    ws = null;
    status.value = "closed";
  }

  function send(payload: unknown): void {
    if (!ws || ws.readyState !== ws.OPEN) return;
    ws.send(JSON.stringify(payload));
  }

  function input(content: string): void {
    send({ t: "input", content });
  }
  function cancel(): void {
    send({ t: "cancel" });
  }

  /**
   * Resolves when the socket reaches OPEN, or rejects on timeout/close.
   * Replaces the previous polling loop in Chat.vue (50ms granularity).
   */
  function whenOpen(timeoutMs = 2000): Promise<void> {
    if (status.value === "open") return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => {
        reject(new Error("timeout waiting for /ws/agents/:name/stream"));
      }, timeoutMs);
      openPromise
        .then(() => {
          clearTimeout(t);
          resolve();
        })
        .catch((e) => {
          clearTimeout(t);
          reject(e instanceof Error ? e : new Error(String(e)));
        });
    });
  }

  onBeforeUnmount(close);

  return { status, open, close, input, cancel, whenOpen };
}
