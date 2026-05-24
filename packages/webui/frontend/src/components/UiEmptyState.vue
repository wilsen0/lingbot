<template>
  <!--
    "此处空空" 通用空态。
    full (默认): 一棵小树 + 一枚铃 + 落瓣 — 用在"页面级空"或第一次进来。
    compact: 仅一行楷书诗题 — 用在 tab 切换、列表空、loading 中间态。
  -->
  <div class="ui-empty" :class="[`ui-empty--${variant}`]" role="status">
    <div v-if="variant === 'full'" class="ui-empty__art" aria-hidden="true">
      <slot name="art">
        <svg viewBox="0 0 140 110" class="ui-empty__svg" aria-hidden="true">
          <defs>
            <linearGradient id="e_bell" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="rgb(var(--color-bell) / 1)" />
              <stop offset="100%" stop-color="rgb(var(--color-bell) / .5)" />
            </linearGradient>
            <radialGradient id="e_petal" cx="40%" cy="40%" r="60%">
              <stop offset="0%" stop-color="#FFE1EA" />
              <stop offset="100%" stop-color="rgb(var(--color-petal))" />
            </radialGradient>
          </defs>
          <path
            d="M10 22 C 40 18 70 30 110 20"
            stroke="rgb(var(--color-ink) / .6)"
            stroke-width="1.8"
            fill="none"
            stroke-linecap="round"
          />
          <path
            d="M30 24 C 40 30 48 42 52 56"
            stroke="rgb(var(--color-ink) / .45)"
            stroke-width="1.3"
            fill="none"
            stroke-linecap="round"
          />
          <path
            d="M70 26 C 72 36, 68 46, 70 56"
            stroke="rgb(var(--color-thread))"
            stroke-width="1.1"
            stroke-dasharray="2.5 3"
            fill="none"
            stroke-linecap="round"
          />
          <g transform="translate(70, 58)">
            <path
              d="M-10 0 a 10 8 0 0 1 20 0 v 8 l 2 2 h -24 l 2 -2 z"
              fill="url(#e_bell)"
              stroke="rgb(var(--color-bell))"
              stroke-width="0.9"
            />
            <path d="M-9 10 h 18" stroke="rgb(var(--color-sorrow) / .6)" stroke-width="1" />
            <circle cx="0" cy="14" r="2.4" fill="rgb(var(--color-sorrow))" />
            <path
              d="M-4 14 q 4 8 -2 14 M 4 14 q -4 8 2 14"
              stroke="rgb(var(--color-thread))"
              stroke-width="1.1"
              fill="none"
              stroke-linecap="round"
            />
          </g>
          <g fill="url(#e_petal)">
            <ellipse cx="22" cy="96" rx="5" ry="2.6" transform="rotate(25 22 96)" opacity="0.9" />
            <ellipse
              cx="48"
              cy="104"
              rx="4"
              ry="2.1"
              transform="rotate(-15 48 104)"
              opacity="0.8"
            />
            <ellipse
              cx="118"
              cy="94"
              rx="5.5"
              ry="2.8"
              transform="rotate(32 118 94)"
              opacity="0.9"
            />
            <ellipse
              cx="100"
              cy="102"
              rx="3.5"
              ry="1.9"
              transform="rotate(-10 100 102)"
              opacity="0.8"
            />
          </g>
        </svg>
      </slot>
    </div>

    <h3 v-if="variant === 'full' || $slots.title" class="ui-empty__title font-display">
      <slot name="title">暂无内容</slot>
    </h3>

    <p
      v-if="$slots.default"
      class="ui-empty__hint"
      :class="{ 'ui-empty__hint--solo': variant === 'compact' && !$slots.title }"
    >
      <slot />
    </p>

    <div v-if="$slots.actions" class="ui-empty__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: "full" | "compact";
  }>(),
  { variant: "full" },
);
</script>

<style scoped>
.ui-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  color: rgb(var(--color-ink-soft));
}
.ui-empty--full {
  padding: 36px 16px;
  gap: 14px;
}
.ui-empty--compact {
  padding: 50px 8px;
}
.ui-empty__art {
  display: block;
  margin: 0 auto 4px;
}
.ui-empty__svg {
  width: 154px;
  height: auto;
  display: block;
  margin: 0 auto;
}
.ui-empty__title {
  font-size: 19px;
  letter-spacing: 0.2em;
  color: rgb(var(--color-ink));
  margin: 0;
  line-height: 1.24;
}
.ui-empty--compact .ui-empty__title {
  font-size: 15px;
}
.ui-empty__hint {
  font-size: 13px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-meta);
  line-height: 1.7;
  margin: 4px 0 0;
  max-width: 34em;
}
.ui-empty__hint--solo {
  /* compact 里只给了 default slot, 没标题 — 字号微调让它独立时也撑得起 */
  font-family: var(--font-display);
  font-size: 15px;
  letter-spacing: 0.28em;
  color: rgb(var(--color-ink-soft));
  margin: 0;
}
.ui-empty__actions {
  margin-top: 16px;
  display: flex;
  gap: 8px;
  justify-content: center;
  flex-wrap: wrap;
}
</style>
