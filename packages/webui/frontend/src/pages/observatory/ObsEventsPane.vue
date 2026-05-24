<template>
  <div class="obs-pane" role="tabpanel">
    <div class="obs-pane__status-row">
      <span class="status" :class="['is-' + status]" aria-live="polite">
        <span class="status__dot" aria-hidden="true">
          <svg
            class="status__thread"
            viewBox="0 0 24 12"
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <path
              d="M0 6 Q 6 0 12 6 T 24 6"
              stroke="currentColor"
              stroke-width="1"
              fill="none"
              stroke-linecap="round"
            />
          </svg>
        </span>
        <span class="status__text">
          <template v-if="status === 'open'">只看自己的消息</template>
          <template v-else-if="status === 'connecting'">连接中</template>
          <template v-else>未连接</template>
        </span>
      </span>
      <span v-if="events.length" class="obs-pane__count font-mono">· {{ events.length }} 条</span>
    </div>

    <UiEmptyState v-if="events.length === 0" variant="compact">
      这里只显示你自己的消息
    </UiEmptyState>
    <ul v-else class="entries">
      <li
        v-for="(ev, i) in events"
        :key="ev.seq"
        class="entry"
        :style="{ animationDelay: i * 18 + 'ms' }"
      >
        <div class="entry__head">
          <span class="entry__kind font-display">{{ eventKindLabel(ev.kind) }}</span>
          <time class="entry__time">{{ formatCompactTime(ev.time) }}</time>
        </div>
        <p class="entry__text">{{ ev.text || "（无文）" }}</p>
        <p class="entry__meta">{{ scopeLabel(ev.scope.kind) }}</p>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";

import UiEmptyState from "@/components/UiEmptyState.vue";

import { formatCompactTime } from "./format";
import { useEvents } from "./useEvents";

const { events, status, start, refresh } = useEvents();

function eventKindLabel(kind: string): string {
  if (kind === "message") return "消息";
  if (kind === "notice") return "通知";
  if (kind === "request") return "请求";
  return "记录";
}

function scopeLabel(kind: string): string {
  if (kind === "dm") return "私聊";
  if (kind === "group") return "群聊";
  return "会话";
}

onMounted(() => {
  refresh();
  start();
});

onBeforeUnmount(() => {
  // useEventStream 自己会在 unmount 时 stop
});
</script>

<style scoped>
.obs-pane {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
}
.obs-pane__status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.obs-pane__count {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 5px 10px 5px 8px;
  background: rgb(var(--color-bg-veil) / 0.42);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 999px;
  box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.05);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: rgb(var(--color-ink-soft));
}
.status__dot {
  position: relative;
  width: 24px;
  height: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: rgb(var(--color-ink-soft) / 0.7);
}
.status__thread {
  width: 24px;
  height: 12px;
  display: block;
}
.status.is-open .status__dot {
  color: rgb(var(--color-jade));
}
.status.is-open .status__thread {
  filter: drop-shadow(0 0 4px rgb(var(--color-jade) / 0.55));
}
.status.is-connecting .status__dot {
  color: rgb(var(--color-thread));
}
.status.is-connecting .status__thread {
  animation: thread-glow 1.4s ease-in-out infinite;
  filter: drop-shadow(0 0 4px rgb(var(--color-thread) / 0.55));
}

.entries {
  display: flex;
  flex-direction: column;
  list-style: none;
  margin: 0;
  padding: 0;
}
.entries > * + * {
  margin-top: 10px;
}

.entry {
  position: relative;
  overflow: hidden;
  padding: 15px 16px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.78), rgb(var(--color-bg-veil) / 0.58));
  backdrop-filter: blur(12px);
  border: 1px solid rgb(var(--color-ink) / 0.055);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / 0.14),
    0 12px 28px rgb(0 0 0 / 0.12),
    inset 0 1px 0 rgb(255 255 255 / 0.06);
  animation: var(--motion-fade-in-up) both;
}
.entry::before {
  content: "";
  position: absolute;
  left: 0;
  top: 14px;
  bottom: 14px;
  width: 2px;
  background: linear-gradient(to bottom, rgb(var(--color-thread) / 0.68), rgb(var(--color-bell) / 0.42));
  border-radius: 2px;
}
.entry__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.entry__kind {
  color: rgb(var(--color-sorrow));
  font-size: 14px;
  letter-spacing: 0.1em;
}
.entry__time {
  font-family: var(--font-mono);
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}
.entry__text {
  color: rgb(var(--color-ink));
  font-size: 15px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.entry__meta {
  margin-top: 6px;
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.06em;
}
</style>
