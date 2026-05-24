<template>
  <section class="obs">
    <nav class="obs__tabs" aria-label="观测面" role="tablist">
      <button
        v-for="t in tabs"
        :key="t.key"
        class="obs__tab tap"
        role="tab"
        :class="{ 'is-active': t.key === active }"
        :aria-selected="t.key === active"
        :aria-label="`${t.label} · ${t.hint}`"
        @click="active = t.key"
      >
        <span class="obs__tab-glyph font-display" aria-hidden="true">{{ t.glyph }}</span>
        <span class="obs__tab-label font-display">{{ t.label }}</span>
        <span class="obs__tab-hint">{{ t.hint }}</span>
      </button>
    </nav>

    <!--
      用 keep-alive 让"切到别的 tab 又切回来"时事件流 / KV 列表的状态保留,
      但仍允许首次进入时按需 mount (delay 加载, 不浪费首屏带宽)。
    -->
    <KeepAlive>
      <ObsEventsPane v-if="active === 'events'" />
      <ObsKvPane v-else-if="active === 'kv'" />
      <ObsAuditPane v-else-if="active === 'audit'" />
    </KeepAlive>
  </section>
</template>

<script setup lang="ts">
import { ref } from "vue";

import ObsAuditPane from "./observatory/ObsAuditPane.vue";
import ObsEventsPane from "./observatory/ObsEventsPane.vue";
import ObsKvPane from "./observatory/ObsKvPane.vue";

type Tab = "events" | "kv" | "audit";
const tabs: { key: Tab; glyph: string; label: string; hint: string }[] = [
  { key: "events", glyph: "己", label: "我的", hint: "记录" },
  { key: "kv",     glyph: "玉", label: "资产", hint: "物品" },
  { key: "audit",  glyph: "志", label: "系统", hint: "记录" },
];
const active = ref<Tab>("events");
</script>

<style scoped>
.obs {
  padding: var(--pad-y) var(--pad-x) calc(env(safe-area-inset-bottom, 0) + 32px);
  width: 100%;
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.obs > * {
  width: min(100%, 780px);
  margin-inline: auto;
}

/* ────── tabs ────── */
.obs__tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin: 8px 0 20px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.82), rgb(var(--color-bg-veil) / 0.58));
  border-radius: var(--radius-paper);
  padding: 6px;
  backdrop-filter: blur(10px);
  border: 1px solid rgb(var(--color-thread) / 0.1);
  box-shadow:
    0 14px 34px rgb(0 0 0 / 0.12),
    inset 0 1px 0 rgb(255 255 255 / 0.08);
}
.obs__tab {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 60px;
  padding: 8px 0;
  background: transparent;
  border: 0;
  color: rgb(var(--color-ink-soft));
  cursor: pointer;
  border-radius: var(--radius-seal);
  transition:
    background var(--dur-base) ease,
    color var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap),
    box-shadow var(--dur-base) ease;
}
.obs__tab-glyph {
  font-size: 22px;
  letter-spacing: 0;
  line-height: 1;
}
.obs__tab-label {
  font-size: 13px;
  letter-spacing: 0.14em;
  line-height: 1;
}
.obs__tab-hint {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: rgb(var(--color-ink-soft) / 0.7);
  line-height: 1;
}
.obs__tab:hover { color: rgb(var(--color-ink)); }
.obs__tab:active { transform: scale(0.97); }
.obs__tab.is-active {
  color: rgb(var(--color-sorrow));
  background: linear-gradient(180deg, rgb(var(--color-sorrow) / 0.16), rgb(var(--color-sorrow) / 0.08));
  box-shadow:
    0 8px 18px rgb(var(--color-sorrow) / 0.12),
    inset 0 1px 0 rgb(255 255 255 / 0.08);
}
.obs__tab.is-active::after {
  content: "";
  position: absolute;
  left: 24%;
  right: 24%;
  bottom: 5px;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-bell) / 0.7), transparent);
}
.obs__tab.is-active .obs__tab-hint {
  color: rgb(var(--color-sorrow) / 0.75);
}
</style>
