<template>
  <component
    :is="tag"
    :type="tag === 'button' ? (type ?? 'button') : undefined"
    :to="to"
    :href="href"
    :disabled="disabled || loading"
    :aria-busy="loading || undefined"
    class="brush-btn tap"
    :class="[kindClass, sizeClass, { 'is-loading': loading }]"
    @click="onClick"
  >
    <DecoBellLoader v-if="loading" :size="spinnerSize" aria-hidden="true" />
    <template v-else>
      <span v-if="showMark" class="brush-btn__mark" aria-hidden="true">·</span>
      <span class="brush-btn__leading"><slot name="leading" /></span>
      <span class="brush-btn__label"><slot /></span>
      <span class="brush-btn__trailing"><slot name="trailing" /></span>
    </template>
  </component>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { RouteLocationRaw } from "vue-router";

import DecoBellLoader from "@/decor/DecoBellLoader.vue";

type Kind = "primary" | "ghost" | "thread" | "danger" | "jade";
type Size = "sm" | "md" | "lg";

const props = withDefaults(
  defineProps<{
    kind?: Kind;
    size?: Size;
    loading?: boolean;
    disabled?: boolean;
    to?: RouteLocationRaw;
    href?: string;
    type?: "button" | "submit" | "reset";
  }>(),
  {
    kind: "primary",
    size: "md",
    loading: false,
    disabled: false,
    to: undefined,
    href: undefined,
    type: "button",
  },
);

const emit = defineEmits<(e: "click", ev: MouseEvent) => void>();

const tag = computed(() => {
  if (props.to) return "router-link";
  if (props.href) return "a";
  return "button";
});

const kindClass = computed(
  () =>
    ({
      primary: "brush-btn--primary",
      ghost: "brush-btn--ghost",
      thread: "brush-btn--thread",
      danger: "brush-btn--danger",
      jade: "brush-btn--jade",
    }[props.kind]),
);

const sizeClass = computed(
  () =>
    ({
      sm: "brush-btn--sm",
      md: "brush-btn--md",
      lg: "brush-btn--lg",
    }[props.size]),
);

const spinnerSize = computed<"sm" | "md">(() => (props.size === "lg" ? "md" : "sm"));

// Ink dot prefix appears on text-like buttons (ghost/thread), not on filled ones.
const showMark = computed(() => props.kind === "ghost" || props.kind === "thread");

function onClick(ev: MouseEvent) {
  if (props.disabled || props.loading) {
    ev.preventDefault();
    return;
  }
  emit("click", ev);
}
</script>

<style scoped>
/*
 * 笔墨按钮：
 *   - primary / danger / jade  ：实色色块（主要动作），非对称 radius
 *   - thread / ghost           ：纯文字，无背景；前缀一点朱墨
 *   所有变体：无边框，按压仅 scale + 颜色变深。
 */
.brush-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-family: var(--font-sans);
  font-weight: 500;
  letter-spacing: 0.08em;
  text-decoration: none;
  cursor: pointer;
  border: 0;
  transition:
    background-color var(--dur-base) ease,
    color var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap),
    opacity var(--dur-base) ease;
  user-select: none;
}
.brush-btn:active:not(:disabled) {
  transform: scale(0.96);
}
.brush-btn:disabled,
.brush-btn.is-loading {
  cursor: not-allowed;
  opacity: 0.5;
}

.brush-btn--sm {
  padding: 6px 10px;
  font-size: 13px;
  min-height: 36px;
}
.brush-btn--md {
  padding: 9px 18px;
  font-size: 14px;
  min-height: 44px;
}
.brush-btn--lg {
  padding: 12px 26px;
  font-size: 16px;
  min-height: 50px;
  letter-spacing: 0.12em;
}

/* ===== 文字型（无背景，仅色差 + 前墨点） ===== */
.brush-btn--ghost {
  background: transparent;
  color: rgb(var(--color-ink-soft));
  padding-left: 6px;
  padding-right: 6px;
}
.brush-btn--ghost:hover:not(:disabled) {
  color: rgb(var(--color-ink));
}

.brush-btn--thread {
  background: transparent;
  color: rgb(var(--color-thread));
  padding-left: 6px;
  padding-right: 6px;
}
.brush-btn--thread:hover:not(:disabled) {
  color: rgb(var(--color-sorrow));
}

.brush-btn__mark {
  color: rgb(var(--color-thread) / 0.7);
  font-size: 18px;
  line-height: 1;
  margin-right: 2px;
}
.brush-btn--ghost .brush-btn__mark { color: rgb(var(--color-ink) / 0.35); }

/* ===== 实色型 · primary 作为主动作 ===== */
.brush-btn--primary {
  background: linear-gradient(180deg, rgb(var(--color-sorrow)) 0%, rgb(var(--color-thread)) 100%);
  color: rgb(var(--color-bg));
  border-radius: var(--radius-seal);
  box-shadow:
    0 1px 0 rgb(255 255 255 / .12) inset,
    0 -1px 0 rgb(0 0 0 / .08) inset,
    0 8px 20px rgb(var(--color-sorrow) / .24);
}
.brush-btn--primary:hover:not(:disabled) {
  filter: brightness(1.03);
}

/* 收笔印记：primary 右下角有一个小墨点，模拟提笔时的"钩" */
.brush-btn--primary::after {
  content: "";
  position: absolute;
  right: 6px;
  bottom: 4px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgb(var(--color-bg) / .35);
  filter: blur(0.3px);
}

.brush-btn--danger {
  background: linear-gradient(180deg, rgb(var(--color-alert)) 0%, rgb(var(--color-sorrow)) 100%);
  color: rgb(var(--color-bg));
  border-radius: var(--radius-seal);
  box-shadow:
    0 1px 0 rgb(255 255 255 / .12) inset,
    0 8px 20px rgb(var(--color-alert) / .22);
}
.brush-btn--danger:hover:not(:disabled) {
  filter: brightness(1.08);
}

.brush-btn--jade {
  background: linear-gradient(180deg, rgb(var(--color-jade)) 0%, rgb(100 180 156) 100%);
  color: rgb(var(--color-bg));
  border-radius: var(--radius-seal);
  box-shadow:
    0 1px 0 rgb(255 255 255 / .12) inset,
    0 8px 20px rgb(var(--color-jade) / .2);
}
.brush-btn--jade:hover:not(:disabled) {
  filter: brightness(1.05);
}

.brush-btn__leading:empty,
.brush-btn__trailing:empty {
  display: none;
}
</style>
