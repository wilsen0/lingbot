<template>
  <header class="chat-header px-safe pt-safe" role="banner">
    <div class="thread-top" />
    <div class="chat-header__inner">
      <button class="icon-btn tap" aria-label="菜单" @click="$emit('open-drawer')">
        <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
          <path
            d="M4 8h16M4 16h10"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
          />
        </svg>
      </button>

      <button
        class="chat-header__agent tap"
        :disabled="!agentCount"
        :aria-haspopup="agentCount > 1 ? 'listbox' : undefined"
        :aria-label="
          currentAgent
            ? `切换红娘 · 当前 ${currentAgent.name}`
            : '选择红娘'
        "
        @click="$emit('pick-agent')"
      >
        <span class="chat-header__agent-name font-display">
          {{ currentAgent?.name ?? loadingLabel }}
        </span>
        <span v-if="currentAgent" class="chat-header__agent-meta">
          <span class="chat-header__agent-mark" aria-hidden="true">♀</span>
        </span>
      </button>

      <button
        class="icon-btn tap"
        :class="{ 'icon-btn--active': !!scope }"
        :aria-label="
          scope ? `切换测试场景 · 当前 ${scope}` : '切换测试场景'
        "
        :title="
          scope
            ? `测试场景：${scope}（点击修改）`
            : '测试场景：默认（你自己的账号）'
        "
        @click="$emit('open-scope')"
      >
        <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
          <circle cx="12" cy="12" r="3.2" stroke="currentColor" stroke-width="1.4" />
          <path
            d="M12 4v3.5M12 16.5V20M4 12h3.5M16.5 12H20"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
          />
        </svg>
      </button>

      <button
        class="icon-btn tap"
        :aria-label="`焚此缘 · 当前 ${messageCount} 句`"
        title="焚此缘 / 重启对话"
        :disabled="!canReset"
        @click="$emit('reset')"
      >
        <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
          <path
            d="M5 12a7 7 0 1 1 2 4.9M5 20v-5h5"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";

import type { AgentSummary } from "@/api/agents";

const props = defineProps<{
  currentAgent: AgentSummary | null;
  agentCount: number;
  loadingAgents: boolean;
  scope: string;
  messageCount: number;
  canReset: boolean;
}>();

defineEmits<{
  (e: "open-drawer"): void;
  (e: "pick-agent"): void;
  (e: "open-scope"): void;
  (e: "reset"): void;
}>();

const loadingLabel = computed(() =>
  props.loadingAgents ? "正请红娘" : "未见红娘",
);
</script>

<style scoped>
.chat-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: linear-gradient(
    to bottom,
    rgb(var(--color-bg) / 0.72),
    rgb(var(--color-bg) / 0.22) 60%,
    rgb(var(--color-bg) / 0)
  );
  backdrop-filter: blur(14px) saturate(130%);
  -webkit-backdrop-filter: blur(14px) saturate(130%);
}
.chat-header__inner {
  display: grid;
  grid-template-columns: 44px 1fr 44px 44px;
  align-items: center;
  gap: var(--gap-sm);
  height: 56px;
  padding: 0 10px;
}

.chat-header__agent {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  background: transparent;
  border: 0;
  color: rgb(var(--color-ink));
  cursor: pointer;
  gap: 2px;
  padding: 0 8px;
  transition: color var(--dur-fast) ease, transform var(--dur-tap) var(--ease-tap);
  position: relative;
}
.chat-header__agent:disabled {
  color: rgb(var(--color-ink-soft));
  cursor: default;
}
.chat-header__agent:active:not(:disabled) {
  transform: scale(0.97);
}
.chat-header__agent:hover:not(:disabled) .chat-header__agent-name {
  color: rgb(var(--color-sorrow));
}
.chat-header__agent-name {
  font-size: clamp(17px, 4.8vw, 20px);
  letter-spacing: var(--track-poem);
  line-height: 1;
  transition: color var(--dur-fast) ease;
  text-shadow: 0 1px 2px rgb(0 0 0 / .35);
}
.chat-header__agent-meta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
}
.chat-header__agent-mark {
  display: inline-block;
  font-size: 11px;
  line-height: 1;
  color: rgb(var(--color-thread) / 0.85);
  letter-spacing: 0;
}
.chat-header__agent::after {
  content: "";
  position: absolute;
  left: 50%;
  bottom: 4px;
  width: clamp(36px, 12vw, 56px);
  height: 1px;
  background: linear-gradient(
    to right,
    transparent,
    rgb(var(--color-thread) / 0.7),
    transparent
  );
  transform: translateX(-50%);
  pointer-events: none;
}
</style>
