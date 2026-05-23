<template>
  <div class="obs-pane" role="tabpanel">
    <UiEmptyState v-if="loading" variant="compact">翻命册……</UiEmptyState>
    <UiEmptyState v-else-if="!rows.length" variant="compact">
      命格未启 · 暂无记
    </UiEmptyState>
    <ul v-else class="entries">
      <li v-for="a in rows" :key="a.id" class="entry">
        <div class="entry__head">
          <span class="entry__kind font-display">{{ a.kind }}</span>
          <time class="entry__time">{{ formatCompactTime(a.time) }}</time>
        </div>
        <p class="entry__meta">
          {{ a.bot_id }} · {{ a.user_id }} ·
          <span :class="a.outcome === 'ok' ? 'ok' : 'err'">{{ a.outcome }}</span>
        </p>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from "vue";

import UiEmptyState from "@/components/UiEmptyState.vue";

import { formatCompactTime } from "./format";
import { useAudit } from "./useAudit";

const { rows, loading, load } = useAudit();

onMounted(load);
</script>

<style scoped>
.obs-pane {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
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
.entry__meta {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
  margin: 0;
}
.entry__meta .ok { color: rgb(var(--color-jade)); }
.entry__meta .err { color: rgb(var(--color-alert)); }
</style>
