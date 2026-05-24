<template>
  <div class="obs-pane" role="tabpanel">
    <UiEmptyState v-if="loading" variant="compact">正在读取系统记录……</UiEmptyState>
    <UiEmptyState v-else-if="!rows.length" variant="compact">
      暂无系统记录
    </UiEmptyState>
    <ul v-else class="entries">
      <li v-for="a in rows" :key="a.id" class="entry">
        <div class="entry__head">
          <span class="entry__kind font-display">{{ auditKindLabel(a.kind) }}</span>
          <time class="entry__time">{{ formatCompactTime(a.time) }}</time>
        </div>
        <p class="entry__meta">
          <span>{{ actorLabel(a.user_id) }}</span>
          <span aria-hidden="true"> · </span>
          <span :class="isOk(a.outcome) ? 'ok' : 'err'">{{ outcomeLabel(a.outcome) }}</span>
          <template v-if="a.latency_ms !== null">
            <span aria-hidden="true"> · </span>
            <span>{{ Math.round(a.latency_ms) }}ms</span>
          </template>
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

function auditKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    handler_dispatch: "指令执行",
    agent_chat: "对话调用",
    event_replay: "事件回放",
    kv_write: "资产调整",
    kv_delete: "资产删除",
  };
  return labels[kind] ?? "系统事件";
}

function outcomeLabel(outcome: string): string {
  if (outcome === "ok") return "成功";
  if (outcome === "ignored") return "已忽略";
  if (outcome === "rate-limited") return "已限流";
  return "失败";
}

function isOk(outcome: string): boolean {
  return outcome === "ok" || outcome === "ignored";
}

function actorLabel(userId: string): string {
  if (!userId) return "系统";
  const masked = /^\d{7,}$/.test(userId)
    ? `${userId.slice(0, 3)}****${userId.slice(-3)}`
    : userId;
  return `操作者 ${masked}`;
}
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
  position: relative;
  overflow: hidden;
  padding: 15px 16px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.78), rgb(var(--color-bg-veil) / 0.58));
  backdrop-filter: blur(12px);
  border: 1px solid rgb(var(--color-ink) / 0.055);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .14),
    0 12px 28px rgb(0 0 0 / .12),
    inset 0 1px 0 rgb(255 255 255 / .06);
  animation: var(--motion-fade-in-up) both;
}
.entry::before {
  content: "";
  position: absolute;
  left: 0;
  top: 14px;
  bottom: 14px;
  width: 2px;
  background: linear-gradient(to bottom, rgb(var(--color-ash) / .72), rgb(var(--color-thread) / .42));
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
.entry__meta {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.06em;
  margin: 0;
}
.entry__meta .ok { color: rgb(var(--color-jade)); }
.entry__meta .err { color: rgb(var(--color-alert)); }
</style>
