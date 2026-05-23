<template>
  <section
    class="ink-card"
    :class="[
      glass ? 'ink-card--glass' : 'ink-card--solid',
      elevated ? 'ink-card--elevated' : '',
      paddingClass,
      { 'ink-card--seal': seal },
    ]"
  >
    <header v-if="title || $slots.header" class="ink-card__header">
      <slot name="header">
        <h3 class="font-serif text-base text-ink tracking-wider">{{ title }}</h3>
        <p v-if="subtitle" class="text-xs text-ink-soft mt-0.5 tracking-wider">{{ subtitle }}</p>
      </slot>
    </header>
    <div class="ink-card__body"><slot /></div>
    <footer v-if="$slots.footer" class="ink-card__footer"><slot name="footer" /></footer>

    <!-- 红印 · 右上角 -->
    <span v-if="seal" class="ink-card__seal" aria-hidden="true">
      <svg viewBox="0 0 36 36" class="w-full h-full">
        <rect
          x="2"
          y="2"
          width="32"
          height="32"
          rx="4"
          fill="rgb(var(--color-sorrow) / .18)"
        />
        <text
          x="18"
          y="14"
          text-anchor="middle"
          fill="rgb(var(--color-sorrow) / .85)"
          style="font-family: var(--font-display); font-size: 10px;"
        >
          {{ sealTextRow1 }}
        </text>
        <text
          x="18"
          y="27"
          text-anchor="middle"
          fill="rgb(var(--color-sorrow) / .85)"
          style="font-family: var(--font-display); font-size: 10px;"
        >
          {{ sealTextRow2 }}
        </text>
      </svg>
    </span>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    title?: string;
    subtitle?: string;
    glass?: boolean;
    elevated?: boolean;
    padding?: "sm" | "md" | "lg";
    seal?: boolean;
    sealText?: string;
  }>(),
  {
    glass: true,
    elevated: false,
    padding: "md",
    title: undefined,
    subtitle: undefined,
    seal: false,
    sealText: "灵",
  },
);

const paddingClass = computed(() => ({ sm: "p-3", md: "p-4", lg: "p-5" }[props.padding]));

const sealTextRow1 = computed(() =>
  props.sealText.length > 1 ? props.sealText[0] : props.sealText,
);
const sealTextRow2 = computed(() =>
  props.sealText.length > 1 ? props.sealText.slice(1, 3) : "",
);
</script>

<style scoped>
/*
 * 无边纸卡：只用柔影与背景浓度区分层级，彻底去掉直线框。
 */
.ink-card {
  position: relative;
  border-radius: var(--radius-paper);
  border: 0;
}
.ink-card--glass {
  background: rgb(var(--color-bg-veil) / 0.78);
  backdrop-filter: blur(14px) saturate(130%);
  -webkit-backdrop-filter: blur(14px) saturate(130%);
  box-shadow: 0 1px 2px rgb(168 20 44 / .04), 0 10px 26px rgb(168 20 44 / .08);
}
.ink-card--solid {
  background: rgb(var(--color-bg-veil));
  box-shadow: 0 1px 2px rgb(0 0 0 / .03), 0 8px 22px rgb(168 20 44 / .06);
}
.ink-card--elevated {
  box-shadow: 0 4px 10px rgb(204 146 64 / .14), 0 16px 42px rgb(204 146 64 / .16);
}

.ink-card__header {
  margin-bottom: 10px;
}
.ink-card__footer {
  margin-top: 14px;
  padding-top: 12px;
  /* 内分割：用柔性阴影而非硬线 */
  background: linear-gradient(
    to right,
    transparent,
    rgb(var(--color-thread) / 0.18),
    transparent
  );
  background-size: 100% 1px;
  background-repeat: no-repeat;
  background-position: top;
}

.ink-card__seal {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 30px;
  height: 30px;
  opacity: 0.9;
  pointer-events: none;
  transform: rotate(6deg);
}
</style>
