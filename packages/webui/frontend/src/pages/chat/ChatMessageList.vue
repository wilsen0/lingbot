<template>
  <main
    ref="rootEl"
    class="msg-list"
    @scroll.passive="onScroll"
  >
    <div class="msg-list__center">
      <slot name="empty" />

      <ul class="msgs" aria-live="polite">
        <li
          v-for="m in messages"
          :key="m.id"
          class="msg"
          :class="['msg--' + m.role]"
        >
          <template v-if="m.role === 'tool'">
            <details class="msg-tool" :open="m.toolName === '↳ 卷'">
              <summary>
                <DecoCompass size="sm" :spinning="isToolPending(m)" />
                <span class="msg-tool__name font-display">
                  {{
                    m.toolName === "↳ 卷"
                      ? "↳ 卷"
                      : `掐指 · ${m.toolName ?? "工具"}`
                  }}
                </span>
                <span class="msg-tool__chevron" aria-hidden="true">›</span>
              </summary>
              <pre class="msg-tool__body">{{ m.content }}</pre>
            </details>
          </template>
          <template v-else>
            <article class="bubble">
              <span class="bubble__corner bubble__corner--tl" aria-hidden="true" />
              <span class="bubble__corner bubble__corner--br" aria-hidden="true" />

              <template v-if="m.segments && m.segments.length">
                <template v-for="(seg, idx) in m.segments" :key="idx">
                  <p
                    v-if="seg.kind === 'text' && seg.text"
                    class="bubble__text"
                  >
                    {{ seg.text }}
                  </p>
                  <img
                    v-else-if="seg.kind === 'image' && seg.url"
                    class="bubble__img"
                    :src="seg.url"
                    :alt="seg.alt || ''"
                    loading="lazy"
                    referrerpolicy="no-referrer"
                    @error="onImgError"
                  />
                </template>
              </template>
              <p v-else class="bubble__text">
                {{ m.content || (m.streaming ? "……" : "") }}
              </p>

              <span
                v-if="m.streaming && m.role === 'assistant'"
                class="bubble__thread"
                aria-hidden="true"
              >
                <svg viewBox="0 0 24 18" preserveAspectRatio="none">
                  <path
                    d="M2 9 q 4 -6 8 0 t 8 0 t 6 0"
                    stroke="rgb(var(--color-thread))"
                    stroke-width="1.1"
                    fill="none"
                    stroke-linecap="round"
                  />
                  <circle cx="22" cy="9" r="2" fill="rgb(var(--color-sorrow))" />
                </svg>
              </span>
            </article>
          </template>
        </li>
        <li v-if="streaming && !lastIsStreamingAssistant" class="msg msg--assistant">
          <article class="bubble bubble--loading">
            <DecoBellLoader size="sm" />
            <span class="bubble__hint">正在牵线……</span>
          </article>
        </li>
      </ul>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import { useRafSchedule } from "@/composables/useRafSchedule";
import DecoBellLoader from "@/decor/DecoBellLoader.vue";
import DecoCompass from "@/decor/DecoCompass.vue";

import type { Msg } from "./types";

const props = defineProps<{
  messages: Msg[];
  streaming: boolean;
  /** 单调递增 token; +1 即说"该考虑滚到底了" — 比 watch 整个数组高效。 */
  newMsgToken: number;
}>();

defineEmits<{
  (e: "stick-changed", stick: boolean): void;
}>();

const rootEl = ref<HTMLElement | null>(null);

const lastIsStreamingAssistant = computed(() => {
  const last = props.messages[props.messages.length - 1];
  return !!last && last.role === "assistant" && last.streaming;
});

/**
 * 一条 tool_call 是否仍在"等结果". 罗盘只在仍在等结果的那一条转,
 * 历史里早已结束的 tool_call 卡片应该静止 — 否则一条会话里调过
 * N 次工具就有 N 个永动 SVG 旋转 + drop-shadow, 移动 GPU 会持续
 * 维持高频时钟, 这是手机发热的隐形大头之一.
 *
 * 规则: 一条 tool_call (toolName !== "↳ 卷") 是 pending, 当且仅当
 *   (a) 整体仍在 streaming
 *   (b) 在它之后没有任何 tool_result ("↳ 卷") 出现
 *
 * 退出 streaming 后所有罗盘自动停下来 — 即使中途某条 call 没拿到
 * result (服务端异常等), 视觉上也不再"虚转".
 */
function isToolPending(m: Msg): boolean {
  if (m.toolName === "↳ 卷") return false;
  if (!props.streaming) return false;
  const idx = props.messages.indexOf(m);
  if (idx < 0) return false;
  for (let i = idx + 1; i < props.messages.length; i++) {
    const n = props.messages[i];
    if (n.role === "tool" && n.toolName === "↳ 卷") return false;
  }
  return true;
}

/**
 * 滚动跟手 / 粘底语义:
 *   - stickToBottom: 用户当前是否粘在底部 (距底 80px 内视为粘底).
 *   - 流式时 newMsgToken ++ → 仅当 stickToBottom 时跟随.
 *   - 用户主动滚远 → 自动失粘, 直到再次拉到底.
 */
const stickToBottom = ref(true);

const scrollSched = useRafSchedule(() => {
  const el = rootEl.value;
  if (el) el.scrollTop = el.scrollHeight;
});

function scrollToBottom(force = false) {
  if (force) stickToBottom.value = true;
  if (!stickToBottom.value) return;
  scrollSched.trigger();
}

function onScroll() {
  const el = rootEl.value;
  if (!el) return;
  stickToBottom.value =
    el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

function onImgError(ev: Event) {
  /*
   * 远程图片失败 — 折叠成一行小提示, 避免气泡里挂浏览器默认破图标。
   * 这是纯展示层兜底, 不向上抛错。
   */
  const img = ev.target as HTMLImageElement | null;
  if (!img) return;
  img.style.display = "none";
  const hint = document.createElement("p");
  hint.className = "bubble__img-fail";
  hint.textContent = "（图片暂时无法加载）";
  img.parentElement?.insertBefore(hint, img);
}

watch(
  () => props.newMsgToken,
  () => nextTick(() => scrollToBottom()),
);

defineExpose({
  /** 父组件强制粘底 — 用户发送 / 切换 agent / 重置后调。 */
  scrollToBottom: () => scrollToBottom(true),
});
</script>

<style scoped>
.msg-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  justify-content: center;
  padding: var(--pad-y) var(--pad-x)
           calc(var(--chat-dock-h, 96px) + 24px + var(--vv-bottom, 0px));
  overscroll-behavior: contain;
  /*
   * 让浏览器原生的"焦点滚到可见区"知道避开输入条 + 键盘。
   * 当输入条聚焦时, scroll-padding 把可见区上移, 焦点不会被 dock 遮住。
   * 这条比手写 scrollIntoView({block:'nearest'}) 鲁棒很多。
   */
  scroll-padding-bottom: calc(var(--chat-dock-h, 96px) + var(--vv-bottom, 0px));
}
.msg-list__center {
  width: 100%;
  max-width: 720px;
  display: flex;
  flex-direction: column;
}

/* 消息 */
.msgs {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
  margin: 0;
  padding: 0;
  list-style: none;
}
/*
 * content-visibility 让屏外的消息跳过样式 / 布局 / paint, 长会话里把
 * 滚动 FPS 抬上来。contain-intrinsic-size 给一个估算占位高度避免滚动条
 * 跳动; 80px 按当前气泡 padding+一行文字估算的中位高度。
 */
.msg {
  display: flex;
  content-visibility: auto;
  contain-intrinsic-size: 0 80px;
}
.msg--user      { justify-content: flex-end; }
.msg--assistant { justify-content: flex-start; }
.msg--tool      { justify-content: center; }

.bubble {
  position: relative;
  max-width: min(86%, 640px);
  padding: 12px 18px;
  font-size: 15px;
  line-height: 1.72;
  letter-spacing: 0.02em;
  color: rgb(var(--color-ink));
  /*
   * 气泡数量随对话长度线性增加, 每个 backdrop-filter 区域在动态底图上
   * 都会被每帧重新模糊 — 长会话里这是手机最大的 GPU 热源之一.
   * 改成提高自身底色不透明度 + inset 高光, 远观与玻璃态视觉差极小,
   * 但完全没有动态模糊成本.
   */
  background: linear-gradient(
    180deg,
    rgb(var(--color-bg-veil) / 0.94) 0%,
    rgb(var(--color-bg-veil) / 0.88) 100%
  );
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .2),
    0 14px 36px rgb(0 0 0 / .14),
    inset 0 1px 0 rgb(255 255 255 / .12),
    inset 0 0 0 1px rgb(255 255 255 / .04);
  animation: var(--motion-fade-in-up);
}
.msg--user .bubble {
  background: linear-gradient(
    135deg,
    rgb(var(--color-sorrow) / 0.52),
    rgb(var(--color-sakura-2) / 0.4)
  );
  color: rgb(var(--color-ink));
  border-radius: 22px 14px 24px 14px;
  box-shadow:
    0 1px 2px rgb(0 0 0 / .22),
    0 14px 36px rgb(var(--color-sorrow) / .22),
    inset 0 1px 0 rgb(255 255 255 / .14);
}

.bubble__corner {
  position: absolute;
  width: 10px;
  height: 10px;
  pointer-events: none;
  opacity: .85;
}
.bubble__corner--tl {
  top: 0;
  left: 0;
  background: linear-gradient(
    135deg,
    rgb(var(--color-thread) / .55) 0%,
    rgb(var(--color-thread) / 0) 70%
  );
  border-top-left-radius: 2px;
}
.bubble__corner--br {
  bottom: 0;
  right: 0;
  background: linear-gradient(
    315deg,
    rgb(var(--color-bell) / .45) 0%,
    rgb(var(--color-bell) / 0) 70%
  );
  border-bottom-right-radius: 2px;
}
.msg--user .bubble .bubble__corner--tl {
  background: linear-gradient(
    135deg,
    rgb(var(--color-bell) / .55) 0%,
    rgb(var(--color-bell) / 0) 70%
  );
}

.bubble__text {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.bubble__text + .bubble__text {
  margin-top: 6px;
}
.bubble__img {
  display: block;
  max-width: 280px;
  max-height: 360px;
  width: auto;
  height: auto;
  border-radius: 12px;
  margin: 6px 0 0;
  box-shadow: 0 4px 12px rgb(0 0 0 / .25);
  background: rgb(var(--color-ink) / .04);
}
.bubble__img:first-child { margin-top: 0; }
.bubble__img-fail {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  font-style: italic;
  letter-spacing: 0.04em;
  margin: 6px 0 0;
}
.bubble--loading {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: rgb(var(--color-ink) / .8);
}
.bubble__hint {
  font-family: var(--font-display);
  font-size: 13px;
  letter-spacing: 0.3em;
}
.bubble__thread {
  position: absolute;
  right: -22px;
  bottom: 8px;
  width: 22px;
  height: 16px;
  pointer-events: none;
  opacity: 0.85;
}
.bubble__thread svg {
  width: 100%;
  height: 100%;
  animation: thread-glow 1.4s ease-in-out infinite;
  filter: drop-shadow(0 0 4px rgb(var(--color-thread) / .55));
  transform-origin: 0% 50%;
}

.msg-tool {
  background: rgb(var(--color-bell) / 0.16);
  border-radius: var(--radius-paper);
  padding: 10px 16px;
  max-width: 92%;
  /* tool 卡数量也随对话长度增长, 同样不能挂 backdrop-filter (见 .bubble) */
  box-shadow: inset 0 1px 0 rgb(255 255 255 / .04);
}
.msg-tool summary {
  cursor: pointer;
  list-style: none;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-display);
  font-size: 13px;
  color: rgb(var(--color-bell));
  letter-spacing: var(--track-meta);
}
.msg-tool summary::-webkit-details-marker { display: none; }
.msg-tool__name { line-height: 1; }
.msg-tool__chevron {
  font-family: var(--font-display);
  color: rgb(var(--color-ink-soft));
  font-size: 16px;
  transition: transform var(--dur-base) var(--ease-stand);
}
.msg-tool[open] .msg-tool__chevron { transform: rotate(90deg); }
.msg-tool__body {
  margin: 8px 0 0;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  /* anywhere 而不是 break-all: 长 URL/路径只在窄到容不下时才折行,
   * 复制时不会被中间硬切, 用户右键复制粘贴更准确。 */
  overflow-wrap: anywhere;
  word-break: normal;
  color: rgb(var(--color-ink) / 0.82);
}

@media (max-width: 420px) {
  .bubble { font-size: 14.5px; padding: 11px 16px; }
}
</style>
