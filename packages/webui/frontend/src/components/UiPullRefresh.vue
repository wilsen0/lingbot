<template>
  <div
    ref="root"
    class="ui-pull-refresh"
    @touchstart.passive="onStart"
    @touchmove.passive="onMove"
    @touchend="onEnd"
  >
    <div
      v-if="pulling || running"
      class="ui-pull-indicator"
      :style="{ transform: `translateY(${Math.max(0, pull - 20)}px)`, opacity: Math.min(1, pull / 60) }"
    >
      <DecoBellLoader size="sm" />
      <span class="text-xs text-ink-soft">{{ running ? "风起时…" : pull > threshold ? "松手牵线" : "下拉刷新" }}</span>
    </div>
    <div
      class="ui-pull-content"
      :style="{ transform: `translateY(${pull * 0.5}px)` }"
    >
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";

import DecoBellLoader from "@/decor/DecoBellLoader.vue";

const props = withDefaults(
  defineProps<{ threshold?: number; onRefresh: () => void | Promise<void> }>(),
  { threshold: 72 },
);

const threshold = props.threshold;
const pull = ref(0);
const pulling = ref(false);
const running = ref(false);
const startY = ref(0);
const root = ref<HTMLElement | null>(null);

function atTop(): boolean {
  if (!root.value) return true;
  // Pull refresh is valid only when the main scroll container is at top.
  const main = root.value.closest("main") as HTMLElement | null;
  const target = main ?? document.scrollingElement ?? document.documentElement;
  return (target as HTMLElement).scrollTop <= 0;
}

function onStart(ev: TouchEvent) {
  if (running.value || !atTop()) return;
  startY.value = ev.touches[0]?.clientY ?? 0;
  pulling.value = true;
}

function onMove(ev: TouchEvent) {
  if (!pulling.value) return;
  const delta = (ev.touches[0]?.clientY ?? 0) - startY.value;
  if (delta > 0) pull.value = Math.min(160, delta);
  else pull.value = 0;
}

async function onEnd() {
  if (!pulling.value) return;
  pulling.value = false;
  if (pull.value >= threshold) {
    running.value = true;
    try {
      await props.onRefresh();
    } finally {
      running.value = false;
    }
  }
  pull.value = 0;
}
</script>

<style scoped>
.ui-pull-refresh {
  position: relative;
}
.ui-pull-indicator {
  position: absolute;
  top: -10px;
  left: 0;
  right: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  pointer-events: none;
  transition: opacity var(--dur-base) ease;
}
.ui-pull-content {
  transition: transform var(--dur-base) ease;
}
@media (prefers-reduced-motion: reduce) {
  .ui-pull-content,
  .ui-pull-indicator {
    transition: none !important;
  }
}
</style>
