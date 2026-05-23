import { ref } from "vue";

import { listEvents, type EventEnvelope } from "@/api/events";
import { useEventStream } from "@/api/ws";

/**
 * "因缘" tab 的事件流 store-like composable.
 *
 * 同时管 REST 历史 (initial load) + WS 流 (live tail), 做了一个 200 上限
 * 的环形截断, 避免长时间挂着把内存吃满。
 *
 * 调用方:
 *   const { events, status, start, refresh } = useEvents();
 *   onMounted(() => { refresh(); start(); });
 */
const CAP = 200;

export function useEvents() {
  const events = ref<EventEnvelope[]>([]);

  const { status, start, stop } = useEventStream({
    onMessage(msg) {
      if (msg.t === "event" && msg.data) {
        events.value.unshift(msg.data as EventEnvelope);
        if (events.value.length > CAP) events.value.length = CAP;
      }
    },
  });

  async function refresh() {
    try {
      const r = await listEvents({ limit: 50 });
      events.value = r.items.slice().reverse();
    } catch {
      // 拉历史失败时保持空, 等 WS 推 live 即可
    }
  }

  return { events, status, start, stop, refresh };
}
