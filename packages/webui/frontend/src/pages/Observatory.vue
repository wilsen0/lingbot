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
  { key: "events", glyph: "因", label: "因缘", hint: "事件流" },
  { key: "kv",     glyph: "玉", label: "灵玉", hint: "键值" },
  { key: "audit",  glyph: "命", label: "命格", hint: "审计" },
];
const active = ref<Tab>("events");
</script>

<style scoped>
.obs {
  padding: var(--pad-y) var(--pad-x) calc(env(safe-area-inset-bottom, 0) + 32px);
  max-width: 720px;
  margin: 0 auto;
}

/* ────── tabs ────── */
.obs__tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin: 8px 0 20px;
  background: rgb(var(--color-bg-veil) / 0.6);
  border-radius: var(--radius-paper);
  padding: 6px;
  backdrop-filter: blur(10px);
  box-shadow: inset 0 0 0 1px rgb(var(--color-thread) / .06);
}
.obs__tab {
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
    transform var(--dur-tap) var(--ease-tap);
}
.obs__tab-glyph {
  font-size: 22px;
  letter-spacing: 0;
  line-height: 1;
}
.obs__tab-label {
  font-size: 13px;
  letter-spacing: 0.22em;
  line-height: 1;
}
.obs__tab-hint {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft) / 0.7);
  line-height: 1;
}
.obs__tab:hover { color: rgb(var(--color-ink)); }
.obs__tab:active { transform: scale(0.97); }
.obs__tab.is-active {
  color: rgb(var(--color-sorrow));
  background: rgb(var(--color-sorrow) / 0.12);
  box-shadow: 0 2px 8px rgb(var(--color-sorrow) / .14);
}
.obs__tab.is-active .obs__tab-hint {
  color: rgb(var(--color-sorrow) / 0.75);
}
</style>
