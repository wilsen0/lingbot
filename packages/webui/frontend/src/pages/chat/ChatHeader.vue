<template>
  <header class="chat-header px-safe pt-safe" role="banner">
    <div class="thread-top" />
    <div class="chat-header__inner">
      <div class="chat-header__top">
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

        <div class="chat-header__center">
          <button
            class="chat-header__agent tap"
            :disabled="!agentCount"
            :aria-haspopup="agentCount > 1 ? 'listbox' : undefined"
            :aria-label="currentAgent ? `切换助手 · 当前 ${currentAgent.name}` : '选择助手'"
            @click="$emit('pick-agent')"
          >
            <span class="chat-header__agent-name font-display">
              {{ currentAgent?.name ?? loadingLabel }}
            </span>
            <span v-if="agentCount > 1" class="chat-header__agent-caret" aria-hidden="true">›</span>
          </button>
        </div>

        <div class="chat-header__actions">
          <button
            class="icon-btn tap"
            :aria-label="`清空对话 · 当前 ${messageCount} 条`"
            title="清空对话"
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
      </div>
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
  messageCount: number;
  canReset: boolean;
}>();

defineEmits<{
  (e: "open-drawer"): void;
  (e: "pick-agent"): void;
  (e: "reset"): void;
}>();

const loadingLabel = computed(() => (props.loadingAgents ? "加载中" : "待接入"));
</script>

<style scoped>
.chat-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: linear-gradient(
    to bottom,
    rgb(var(--color-bg) / 0.76) 0%,
    rgb(var(--color-bg) / 0.36) 62%,
    rgb(var(--color-bg) / 0)
  );
  backdrop-filter: blur(14px) saturate(125%);
  -webkit-backdrop-filter: blur(14px) saturate(125%);
}
.chat-header__inner {
  max-width: 860px;
  margin: 0 auto;
  padding: 8px 12px;
}
.chat-header__top {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 46px;
}
.chat-header__actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.chat-header .icon-btn {
  background: rgb(var(--color-bg-veil) / 0.28);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / 0.05),
    0 8px 18px rgb(0 0 0 / 0.08);
}
.chat-header__center {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chat-header__agent {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
  min-height: 38px;
  max-width: min(46vw, 360px);
  background: rgb(var(--color-bg-veil) / 0.22);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 999px;
  color: rgb(var(--color-ink));
  cursor: pointer;
  gap: 6px;
  padding: 0 13px 1px;
  transition:
    background var(--dur-fast) ease,
    border-color var(--dur-fast) ease,
    color var(--dur-fast) ease,
    transform var(--dur-tap) var(--ease-tap);
  position: relative;
  box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.05);
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
  font-size: clamp(15px, 4.4vw, 18px);
  letter-spacing: 0.08em;
  line-height: 1;
  transition: color var(--dur-fast) ease;
  text-shadow: 0 1px 2px rgb(0 0 0 / 0.35);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: min(44vw, 360px);
}
.chat-header__agent-caret {
  color: rgb(var(--color-thread));
  font-family: var(--font-display);
  font-size: 18px;
  line-height: 1;
  transform: translateY(-1px) rotate(90deg);
}

@media (max-width: 420px) {
  .chat-header__inner {
    padding-inline: 8px;
  }
  .chat-header__top {
    gap: 6px;
  }
  .chat-header__actions {
    gap: 4px;
  }
  .chat-header__agent-name {
    letter-spacing: 0.12em;
    max-width: 34vw;
  }
}
@media (prefers-reduced-motion: reduce) {
  .chat-header__agent,
  .chat-header .icon-btn {
    transition: none;
  }
}
</style>
