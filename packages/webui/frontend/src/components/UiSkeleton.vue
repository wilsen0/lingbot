<template>
  <div class="ui-skeleton" :style="style" aria-hidden="true" />
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    width?: string;
    height?: string;
    rounded?: string;
  }>(),
  { width: "100%", height: "1em", rounded: "var(--radius-sm)" },
);

const style = computed(() => ({
  width: props.width,
  height: props.height,
  borderRadius: props.rounded,
}));
</script>

<style scoped>
.ui-skeleton {
  position: relative;
  overflow: hidden;
  background: rgb(var(--color-ink-soft) / 0.08);
}
.ui-skeleton::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(
    110deg,
    transparent 30%,
    rgb(var(--color-petal) / 0.35) 50%,
    transparent 70%
  );
  animation: skeleton-shimmer 1.6s linear infinite;
}
@keyframes skeleton-shimmer {
  0% {
    transform: translateX(-100%);
  }
  100% {
    transform: translateX(100%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .ui-skeleton::after {
    animation: none;
  }
}
</style>
