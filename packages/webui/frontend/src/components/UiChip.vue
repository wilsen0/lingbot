<template>
  <button
    type="button"
    class="brush-chip tap"
    :class="[{ 'is-active': active }, toneClass]"
    :aria-pressed="active ? 'true' : 'false'"
  >
    <span class="brush-chip__mark" aria-hidden="true">·</span>
    <span class="brush-chip__label"><slot /></span>
  </button>
</template>

<script setup lang="ts">
import { computed } from "vue";

type Tone = "thread" | "bell" | "jade" | "ash";

const props = withDefaults(
  defineProps<{ active?: boolean; tone?: Tone }>(),
  { active: false, tone: "thread" },
);

const toneClass = computed(() => `brush-chip--${props.tone}`);
</script>

<style scoped>
/*
 * 墨字式 chip：
 *   - 非选中：纯文字，前一点朱墨。背景完全透明。
 *   - 选中  ：底下一抹墨色笔锋（::after）+ 文字变苦情红。
 * 不再是独立色块。
 */
.brush-chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px 10px 10px;
  min-height: 44px;
  background: transparent;
  border: 0;
  color: rgb(var(--color-ink-soft));
  font-size: 14px;
  letter-spacing: 0.1em;
  cursor: pointer;
  transition: color var(--dur-fast) ease, transform var(--dur-tap) var(--ease-tap);
}
.brush-chip:active:not(:disabled) {
  transform: translateY(1px);
}
.brush-chip__mark {
  font-size: 16px;
  line-height: 1;
  color: rgb(var(--color-thread) / 0.55);
  transition: color var(--dur-fast) ease, transform var(--dur-fast) ease;
}
.brush-chip:hover {
  color: rgb(var(--color-ink));
}
.brush-chip:hover .brush-chip__mark {
  color: rgb(var(--color-thread));
}

/* 笔锋：选中时下方一抹斜向的墨（SVG-shaped gradient） */
.brush-chip::after {
  content: "";
  position: absolute;
  left: 12%;
  right: 12%;
  bottom: 2px;
  height: 3px;
  background: linear-gradient(
    to right,
    transparent 0%,
    rgb(var(--color-sorrow) / 0) 10%,
    rgb(var(--color-sorrow)) 35%,
    rgb(var(--color-sorrow)) 70%,
    rgb(var(--color-sorrow) / 0) 95%
  );
  transform: scaleX(0) skewX(-6deg);
  transform-origin: center;
  transition: transform var(--dur-stage) var(--ease-firm);
  filter: drop-shadow(0 1px 2px rgb(var(--color-sorrow) / 0.35));
  border-radius: 2px;
}

.brush-chip.is-active {
  color: rgb(var(--color-sorrow));
}
.brush-chip.is-active .brush-chip__mark {
  color: rgb(var(--color-sorrow));
  transform: scale(1.15);
}
.brush-chip.is-active::after {
  transform: scaleX(1) skewX(-6deg);
}

/* 调色派生 */
.brush-chip--bell.is-active { color: rgb(var(--color-bell)); }
.brush-chip--bell.is-active::after {
  background: linear-gradient(
    to right,
    transparent, rgb(var(--color-bell) / 0) 10%,
    rgb(var(--color-bell)) 35%, rgb(var(--color-bell)) 70%,
    rgb(var(--color-bell) / 0) 95%, transparent
  );
  filter: drop-shadow(0 1px 2px rgb(var(--color-bell) / 0.35));
}
.brush-chip--jade.is-active { color: rgb(var(--color-jade)); }
.brush-chip--jade.is-active::after {
  background: linear-gradient(
    to right,
    transparent, rgb(var(--color-jade) / 0) 10%,
    rgb(var(--color-jade)) 35%, rgb(var(--color-jade)) 70%,
    rgb(var(--color-jade) / 0) 95%, transparent
  );
  filter: drop-shadow(0 1px 2px rgb(var(--color-jade) / 0.3));
}
.brush-chip--ash.is-active { color: rgb(var(--color-ink-soft)); }

@media (prefers-reduced-motion: reduce) {
  .brush-chip,
  .brush-chip__mark,
  .brush-chip::after {
    transition: none !important;
  }
}
</style>
