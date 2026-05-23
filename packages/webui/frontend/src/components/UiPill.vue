<template>
  <span class="ink-pill" :class="toneClass">
    <span v-if="dot" class="ink-pill__dot" aria-hidden="true" />
    <slot />
  </span>
</template>

<script setup lang="ts">
import { computed } from "vue";

type Tone = "sorrow" | "thread" | "bell" | "jade" | "alert" | "ash";

const props = withDefaults(defineProps<{ tone?: Tone; dot?: boolean }>(), {
  tone: "thread",
  dot: false,
});

const toneClass = computed(() => `ink-pill--${props.tone}`);
</script>

<style scoped>
.ink-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 6px 12px 8px 14px;
  font-size: 11px;
  line-height: 1.4;
  font-weight: 500;
  letter-spacing: 0.08em;
}
.ink-pill__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.ink-pill--sorrow {
  background: rgb(var(--color-sorrow) / 0.14);
  color: rgb(var(--color-sorrow));
}
.ink-pill--thread {
  background: rgb(var(--color-thread) / 0.14);
  color: rgb(var(--color-thread));
}
.ink-pill--bell {
  background: rgb(var(--color-bell) / 0.2);
  color: rgb(var(--color-bell));
}
.ink-pill--jade {
  background: rgb(var(--color-jade) / 0.18);
  color: rgb(var(--color-jade));
}
.ink-pill--alert {
  background: rgb(var(--color-alert) / 0.16);
  color: rgb(var(--color-alert));
}
.ink-pill--ash {
  background: rgb(var(--color-ink) / 0.06);
  color: rgb(var(--color-ink-soft));
}
</style>
