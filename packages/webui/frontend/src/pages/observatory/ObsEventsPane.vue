<template>
  <div class="obs-pane" role="tabpanel">
    <div class="obs-pane__status-row">
      <span class="status" :class="['is-' + status]" aria-live="polite">
        <span class="status__dot" aria-hidden="true">
          <svg class="status__thread" viewBox="0 0 24 12" preserveAspectRatio="none" aria-hidden="true">
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
          <template v-if="status === 'open'">红线已牵</template>
          <template v-else-if="status === 'connecting'">正在牵线</template>
          <template v-else>风未起</template>
        </span>
      </span>
      <span v-if="events.length" class="obs-pane__count font-mono">· {{ events.length }} 条</span>
    </div>

    <UiEmptyState v-if="events.length === 0" variant="compact">
      风未起 · 铃未响
    </UiEmptyState>
    <ul v-else class="entries">
      <li
        v-for="(ev, i) in events"
        :key="ev.seq"
        class="entry"
        :style="{ animationDelay: i * 18 + 'ms' }"
      >
        <div class="entry__head">
          <span class="entry__kind font-display">{{ ev.kind }}</span>
          <time class="entry__time">{{ formatCompactTime(ev.time) }}</time>
        </div>
        <p class="entry__text">{{ ev.text || "（无文）" }}</p>
        <p class="entry__meta">
          {{ ev.platform }} · {{ ev.scope.kind }}/{{ ev.scope.id }}
        </p>
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
  font-size: 11px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink-soft));
}
.status__dot {
  position: relative;
  width: 24px;
  height: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: rgb(var(--color-ink-soft) / .7);
}
.status__thread {
  width: 24px;
  height: 12px;
  display: block;
}
.status.is-open .status__dot { color: rgb(var(--color-jade)); }
.status.is-open .status__thread {
  filter: drop-shadow(0 0 4px rgb(var(--color-jade) / .55));
}
.status.is-connecting .status__dot { color: rgb(var(--color-thread)); }
.status.is-connecting .status__thread {
  animation: thread-glow 1.4s ease-in-out infinite;
  filter: drop-shadow(0 0 4px rgb(var(--color-thread) / .55));
}

.entries {
  display: flex;
  flex-direction: column;
  list-style: none;
  margin: 0;
  padding: 0;
}
.entries > * + * { margin-top: 10px; }

.entry {
  padding: 14px 16px;
  background: rgb(var(--color-bg-veil) / 0.62);
  backdrop-filter: blur(12px);
  border-radius: var(--radius-paper);
  box-shadow: 0 1px 2px rgb(0 0 0 / .14), 0 8px 24px rgb(0 0 0 / .12);
  animation: var(--motion-fade-in-up) both;
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
  letter-spacing: var(--track-meta);
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
  letter-spacing: var(--track-fn);
}
</style>
