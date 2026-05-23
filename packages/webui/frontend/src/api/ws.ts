import { onBeforeUnmount, ref, type Ref } from "vue";

import { useAuthStore } from "@/store/auth";

export interface EventStreamMessage {
  t: "hello" | "event" | "filter_ack" | "ping";
  bot_id?: string;
  data?: unknown;
  replayed?: number;
  capacity?: number;
  server_time?: number;
}

export interface UseEventStreamOptions {
  onMessage?: (msg: EventStreamMessage) => void;
  autoReconnect?: boolean;
}

function wsBase(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

export function useEventStream(opts: UseEventStreamOptions = {}) {
  const auth = useAuthStore();
  const status: Ref<"connecting" | "open" | "closed" | "reconnecting"> = ref("closed");
  const lastSeq: Ref<number | null> = ref(null);
  let ws: WebSocket | null = null;
  let pingTimer: ReturnType<typeof setTimeout> | null = null;
  let backoff = 800;

  function cleanup() {
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = null;
    ws?.close();
    ws = null;
  }

  function connect() {
    if (!auth.accessToken) return;
    status.value = ws ? "reconnecting" : "connecting";
    const url = `${wsBase()}/ws/events?token=${encodeURIComponent(auth.accessToken)}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      status.value = "open";
      backoff = 800;
      if (lastSeq.value != null) {
        ws?.send(JSON.stringify({ t: "filter", data: { since_seq: lastSeq.value } }));
      }
      pingTimer = setInterval(() => {
        try {
          ws?.send(JSON.stringify({ t: "ping" }));
        } catch {
          /* no-op */
        }
      }, 15_000);
    };

    ws.onmessage = (ev) => {
      let msg: EventStreamMessage | null = null;
      try {
        msg = JSON.parse(ev.data) as EventStreamMessage;
      } catch {
        return;
      }
      if (!msg) return;
      if (msg.t === "event" && msg.data && typeof msg.data === "object") {
        const seq = (msg.data as { seq?: number }).seq;
        if (typeof seq === "number") lastSeq.value = seq;
      }
      opts.onMessage?.(msg);
    };

    ws.onclose = () => {
      status.value = "closed";
      cleanup();
      if (opts.autoReconnect !== false && auth.isAuthed) {
        backoff = Math.min(backoff * 1.6, 12_000);
        setTimeout(connect, backoff);
      }
    };

    ws.onerror = () => {
      /* handled via onclose */
    };
  }

  function start() {
    if (ws) return;
    connect();
  }
  function stop() {
    cleanup();
    status.value = "closed";
  }

  onBeforeUnmount(stop);

  return { status, lastSeq, start, stop };
}
