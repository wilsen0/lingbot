<template>
  <span class="decor-bell-loader inline-flex items-center justify-center" :class="sizeClass" :aria-label="label">
    <!--
      与 Login.vue 顶端的吊铃 / UiSwitch 的铃 / 全站 bell-swing 共用同一支
      keyframe 与曲线（var(--motion-bell-loader)）, 视觉风格保持一致。
    -->
    <svg viewBox="0 0 24 24" fill="none" class="origin-top decor-bell-loader__svg">
      <!-- 铃身 -->
      <path
        d="M12 3c-.6 0-1 .4-1 1v.6a5.5 5.5 0 0 0-4 5.3V14l-1.3 1.3a1 1 0 0 0 .7 1.7h11.2a1 1 0 0 0 .7-1.7L17 14v-4.1a5.5 5.5 0 0 0-4-5.3V4c0-.6-.4-1-1-1z"
        stroke="currentColor"
        stroke-width="1.2"
        stroke-linejoin="round"
        fill="rgb(var(--color-bell) / .24)"
      />
      <!-- 铃舌 -->
      <circle cx="12" cy="19" r="1.6" fill="currentColor" />
    </svg>
  </span>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    size?: "sm" | "md" | "lg";
    label?: string;
  }>(),
  { size: "md", label: "加载中" },
);

const sizeClass = computed(() => ({
  "w-4 h-4 text-bell": props.size === "sm",
  "w-6 h-6 text-bell": props.size === "md",
  "w-10 h-10 text-bell": props.size === "lg",
}));
</script>

<style scoped>
.decor-bell-loader__svg {
  /* 全站统一的"风中铃"摆动, loader 节奏比装饰用快 (1.2s) */
  animation: var(--motion-bell-loader);
}

@media (prefers-reduced-motion: reduce) {
  .decor-bell-loader__svg {
    animation: none;
  }
}
</style>
