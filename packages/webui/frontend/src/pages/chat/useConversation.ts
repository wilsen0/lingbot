import { computed, onBeforeUnmount, ref } from "vue";

import { useAgentStream } from "@/api/agentStream";
import { useBellSound } from "@/composables/useBellSound";
import { useHaptics } from "@/composables/useHaptics";
import { useRafSchedule } from "@/composables/useRafSchedule";
import { useStageBus } from "@/composables/useStageBus";
import { toast } from "@/composables/useToast";

import type { Msg, MsgSegment } from "./types";

/**
 * 会话控制器 — Chat 子系统的"模型"层。
 *
 * Chat.vue 重构前 1400+ 行的根因, 是把 WS 生命周期、消息状态、流式合并、
 * tool 气泡入队、完成态副作用 (铃响 / 震感 / 舞台铃) 全塞在 setup() 里。
 * 这里独立成一份可单测的 composable:
 *
 *   - openFor(name)  打开 / 切换 stream
 *   - close()        关掉
 *   - send(text)     发, 维护 user 气泡 + 进入 streaming 状态
 *   - cancel()       取消当前流, streaming 标志归零
 *   - reset()        焚此缘 — 清空消息 + 取消未决 stream
 *
 * 暴露的状态:
 *   - messages       Msg 列表
 *   - streaming      是否在等回复
 *   - status         WS 连接态
 *   - newMsgToken    单调递增, 视图层 watch 它即可"该贴底了"
 *
 * 视图层 (Chat.vue / ChatMessageList) 只负责消费 messages + 反应 streaming,
 * 不再持有 stream 句柄 / msgId 计数等内部状态。
 */
export function useConversation() {
  // ───────────────────────── public state ─────────────────────────
  const messages = ref<Msg[]>([]);
  const streaming = ref(false);
  /**
   * 单调递增 token. 视图侧 watch 它就能感知"有新消息进来 / 新 delta 到了",
   * 不需要把整个 messages 数组当 watch 源 (那会触发深比对). O(1) 信号.
   */
  const newMsgToken = ref(0);
  function bumpNew() {
    newMsgToken.value += 1;
  }

  // ───────────────────────── side-effects ─────────────────────────
  const haptics = useHaptics();
  const bell = useBellSound();
  const stage = useStageBus();

  // ───────────────────────── stream binding ─────────────────────────
  let stream: ReturnType<typeof useAgentStream> | null = null;
  /** 当前会话连接到的 agent 名 — close 时和 stream 一起置 null */
  const currentAgent = ref<string | null>(null);

  const status = computed(() => (stream ? stream.status.value : ("closed" as const)));

  let msgIdCounter = 1;
  function nextId() {
    return msgIdCounter++;
  }

  // ───────────────────────── delta queue ─────────────────────────
  /**
   * 流式 delta 合并队列. 模型快速产文时会以 10–50 段/秒发 token, 直接每段
   * push 进数组会让 Vue 反应式系统每帧反复 patch, 长会话明显掉帧。
   * 同一帧内到达的 delta 合并后再一次性写入 — pendingAssistantId 为空时
   * 创建新气泡, 否则追加到既有气泡。
   */
  const queue = (() => {
    let pending = "";
    let assistantId: number | null = null;

    function flush() {
      if (!pending) return;
      const text = pending;
      pending = "";
      if (assistantId === null) {
        const id = nextId();
        assistantId = id;
        messages.value.push({
          id,
          role: "assistant",
          content: text,
          streaming: true,
        });
      } else {
        const cur = messages.value.find((m) => m.id === assistantId);
        if (cur) cur.content += text;
      }
      bumpNew();
    }

    const sched = useRafSchedule(flush);

    return {
      push(text: string) {
        if (!text) return;
        pending += text;
        sched.trigger();
      },
      drain: sched.flush,
      reset() {
        sched.cancel();
        pending = "";
        assistantId = null;
      },
      finishAssistant(meta?: { segments?: MsgSegment[] }) {
        sched.flush();
        if (assistantId === null) return;
        const cur = messages.value.find((m) => m.id === assistantId);
        if (cur) {
          cur.streaming = false;
          if (meta?.segments?.length) {
            cur.segments = meta.segments
              .filter((s) => (s.kind === "text" && s.text) || (s.kind === "image" && s.url))
              .map((s) => ({
                kind: s.kind,
                text: s.text ?? "",
                url: s.url ?? "",
                alt: s.alt ?? "",
              }));
          }
        }
        assistantId = null;
      },
      abortAssistant() {
        sched.flush();
        if (assistantId === null) return;
        const cur = messages.value.find((m) => m.id === assistantId);
        if (cur) cur.streaming = false;
        assistantId = null;
      },
    };
  })();

  // ───────────────────────── helpers ─────────────────────────
  /** tool_call.args / tool_result.result 序列化: string 直传, 否则 pretty-print。 */
  function toToolPayload(v: unknown): string {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  }

  function pushTool(toolName: string, payload: unknown) {
    queue.drain(); // 维持时序: 先把累积 delta 落盘
    messages.value.push({
      id: nextId(),
      role: "tool",
      toolName,
      content: toToolPayload(payload),
    });
    bumpNew();
  }

  // ───────────────────────── public API ─────────────────────────
  function openFor(agentName: string) {
    if (currentAgent.value === agentName && stream) return;
    stream?.close();
    currentAgent.value = agentName;
    stream = useAgentStream(agentName, {
      onMessage(msg) {
        switch (msg.t) {
          case "delta":
            queue.push(msg.text);
            return;
          case "tool_call":
            pushTool(msg.name, msg.args);
            return;
          case "tool_result":
            pushTool("↳ 卷", msg.result);
            return;
          case "done":
            queue.finishAssistant({ segments: msg.segments });
            streaming.value = false;
            bell.ring();
            stage.ringBell();
            haptics.tap(6);
            bumpNew();
            return;
          case "error":
            queue.abortAssistant();
            streaming.value = false;
            toast.error("助手暂时没有回应", msg.msg);
            return;
        }
      },
    });
    stream.open();
  }

  function close() {
    stream?.close();
    stream = null;
    currentAgent.value = null;
  }

  async function send(text: string): Promise<boolean> {
    const t = text.trim();
    if (!t) return false;
    if (!currentAgent.value || !stream) return false;
    if (streaming.value) return false;

    messages.value.push({ id: nextId(), role: "user", content: t });
    bumpNew();

    streaming.value = true;
    queue.reset();
    haptics.tap(8);

    try {
      await stream.whenOpen(2000);
      stream.input(t);
      return true;
    } catch (e) {
      streaming.value = false;
      toast.error("连接未就绪", (e as Error).message || "请稍后再试");
      return false;
    }
  }

  function cancel() {
    stream?.cancel();
    queue.abortAssistant();
    streaming.value = false;
  }

  function reset() {
    cancel();
    messages.value = [];
    queue.reset();
    haptics.tap(8);
    bumpNew();
  }

  onBeforeUnmount(() => {
    queue.reset();
    close();
  });

  return {
    messages,
    streaming,
    status,
    currentAgent,
    newMsgToken,
    openFor,
    close,
    send,
    cancel,
    reset,
  };
}
