<template>
  <!--
    "红娘掐指" · 一只缓慢旋转的罗盘 / 算筹组合，
    用作 tool_call 气泡的导引图标。
  -->
  <span
    class="deco-compass"
    :class="['is-' + size, spinning ? 'is-spinning' : '']"
    :aria-label="label"
    :title="label"
  >
    <svg viewBox="0 0 28 28" fill="none">
      <!-- 外圈 -->
      <circle cx="14" cy="14" r="12.5" stroke="currentColor" stroke-width="1.1" opacity=".7" />
      <!-- 八卦点 -->
      <g stroke="currentColor" stroke-width="0.8" opacity=".55">
        <line x1="14" y1="2" x2="14" y2="5" />
        <line x1="14" y1="23" x2="14" y2="26" />
        <line x1="2" y1="14" x2="5" y2="14" />
        <line x1="23" y1="14" x2="26" y2="14" />
        <line x1="5.5" y1="5.5" x2="7.5" y2="7.5" />
        <line x1="20.5" y1="20.5" x2="22.5" y2="22.5" />
        <line x1="22.5" y1="5.5" x2="20.5" y2="7.5" />
        <line x1="7.5" y1="20.5" x2="5.5" y2="22.5" />
      </g>
      <!-- 内圆 (太极意象) -->
      <circle cx="14" cy="14" r="6" stroke="currentColor" stroke-width="0.7" opacity=".4" />
      <!-- 罗盘指针 · 旋转层 -->
      <g class="deco-compass__needle">
        <path d="M14 4 L 16 14 L 14 24 L 12 14 Z" fill="rgb(var(--color-thread))" opacity="0.85" />
        <circle cx="14" cy="14" r="1.4" fill="rgb(var(--color-bell))" />
      </g>
    </svg>
  </span>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    size?: "sm" | "md";
    spinning?: boolean;
    label?: string;
  }>(),
  { size: "md", spinning: true, label: "正在处理" },
);
</script>

<style scoped>
.deco-compass {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: rgb(var(--color-bell));
}
.is-sm svg {
  width: 16px;
  height: 16px;
}
.is-md svg {
  width: 22px;
  height: 22px;
}

.deco-compass__needle {
  transform-origin: 14px 14px;
  transition: transform var(--dur-base) var(--ease-stand);
}
.is-spinning .deco-compass__needle {
  animation: compass-spin 2.4s linear infinite;
}

@keyframes compass-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-spinning .deco-compass__needle {
    animation: none;
    transform: rotate(45deg);
  }
}
</style>
