<template>
  <div v-if="visible" class="suggest" role="listbox" :aria-label="ariaLabel">
    <ul class="suggest__list">
      <li
        v-for="(item, idx) in results"
        :key="item.raw"
        class="suggest__item"
        :class="{ 'is-active': activeIdx === idx }"
        :aria-selected="activeIdx === idx"
        role="option"
        @pointerdown.prevent="$emit('pick', item)"
        @mousemove="$emit('hover', idx)"
      >
        <span class="suggest__label">
          <template v-for="(seg, i) in highlightSegments(item, query)" :key="i">
            <mark v-if="seg.hit" class="suggest__hit">{{ seg.text }}</mark>
            <template v-else>{{ seg.text }}</template>
          </template>
        </span>
        <span v-if="item.has_args" class="suggest__hint font-display" aria-hidden="true">参数</span>
      </li>
    </ul>
    <p v-if="results.length" class="suggest__foot font-display" aria-hidden="true">
      ↑↓ 选择 · Enter 发送 · Esc 关闭
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

import type { TriggerSuggestion } from "@/api/agents";

/**
 * Inline-suggest panel pinned above the composer.
 *
 * Pure-presentational. ``ChatComposer`` owns the data + keyboard
 * model (``useTriggerSuggest``); this component just renders. We
 * keep pointerdown's preventDefault on the row click so picking a
 * suggestion doesn't blur the textarea (which would steal focus
 * and close the panel before the click resolves).
 *
 * The label highlights the matching prefix with a subtle accent —
 * just enough to confirm "I know what you typed" without making
 * the panel look like a dictionary. We render via plain text
 * bindings (not v-html) so the user's query never reaches the DOM
 * as markup, eliminating the XSS surface entirely.
 */
const props = defineProps<{
  visible: boolean;
  results: TriggerSuggestion[];
  /** -1 means "no explicit highlight". */
  cursor: number;
  query: string;
}>();

defineEmits<{
  (e: "pick", item: TriggerSuggestion): void;
  (e: "hover", idx: number): void;
}>();

const activeIdx = computed(() => props.cursor);

const ariaLabel = computed(() =>
  props.results.length === 1 ? "一条候选" : `${props.results.length} 条候选`,
);

interface LabelSegment {
  text: string;
  hit: boolean;
}

/** Split ``label`` around the first case-insensitive occurrence of
 * ``query``. Returning a small struct array (instead of an HTML
 * string) lets the template render via normal Vue text bindings —
 * no v-html, no escape dance, no XSS surface.
 *
 * Two-segment max output is intentional: only the first hit is
 * highlighted. QRDic triggers are short (most under 8 chars), so
 * multiple-hit highlighting would be visual noise. */
function highlightSegments(item: TriggerSuggestion, query: string): LabelSegment[] {
  const label = item.label;
  const q = query.trim().toLowerCase();
  if (!q) return [{ text: label, hit: false }];
  const idx = label.toLowerCase().indexOf(q);
  if (idx < 0) return [{ text: label, hit: false }];
  const out: LabelSegment[] = [];
  if (idx > 0) out.push({ text: label.slice(0, idx), hit: false });
  out.push({ text: label.slice(idx, idx + q.length), hit: true });
  if (idx + q.length < label.length) {
    out.push({ text: label.slice(idx + q.length), hit: false });
  }
  return out;
}
</script>

<style scoped>
.suggest {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  bottom: 100%;
  margin-bottom: 6px;
  width: min(calc(100% - 2 * var(--pad-x)), 780px);
  max-width: 780px;
  pointer-events: auto;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.98) 0%, rgb(var(--color-bg-veil) / 0.9) 100%);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border: 1px solid rgb(var(--color-thread) / 0.12);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / 0.25),
    0 16px 36px rgb(0 0 0 / 0.2),
    inset 0 1px 0 rgb(255 255 255 / 0.1);
  padding: 7px;
  z-index: 11;
  animation: var(--motion-fade-in-up);
  overflow: hidden;
}
.suggest::before {
  content: "";
  position: absolute;
  left: 8%;
  right: 8%;
  top: 0;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-bell) / 0.42), transparent);
  pointer-events: none;
}
.suggest__list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: max(
    96px,
    min(36vh, calc(100svh - var(--vv-bottom, 0px) - var(--chat-dock-h, 96px) - 112px))
  );
  overflow-y: auto;
  overscroll-behavior: contain;
  /* 隐藏滚动条但保留滚动能力 — 候选最多 8 条, 滚一下就到底,
   * 那条粗黑的浏览器默认滚动条挂在淡彩面板上太刺眼. */
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.suggest__list::-webkit-scrollbar {
  display: none;
}
.suggest__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: calc(var(--radius-paper) - 4px);
  color: rgb(var(--color-ink));
  cursor: pointer;
  font-size: 14px;
  letter-spacing: 0.02em;
  line-height: 1.4;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease;
}
.suggest__item:hover,
.suggest__item.is-active {
  background: linear-gradient(90deg, rgb(var(--color-thread) / 0.16), rgb(var(--color-sorrow) / 0.1));
  color: rgb(var(--color-sorrow));
  box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.06);
}
.suggest__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.suggest__hit {
  background: transparent;
  color: rgb(var(--color-sorrow));
  font-weight: 600;
}
.suggest__hint {
  flex-shrink: 0;
  font-size: 11px;
  letter-spacing: 0.14em;
  color: rgb(var(--color-ink-soft));
  padding: 2px 7px;
  background: rgb(var(--color-bg) / 0.22);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 999px;
}
.suggest__item.is-active .suggest__hint {
  color: rgb(var(--color-sorrow) / 0.72);
  background: rgb(var(--color-sorrow) / 0.08);
}
.suggest__foot {
  margin: 4px 6px 2px;
  padding-top: 6px;
  border-top: 1px solid rgb(var(--color-ink) / 0.06);
  font-size: 10px;
  letter-spacing: 0.14em;
  color: rgb(var(--color-ink-soft));
  text-align: right;
}

@media (prefers-reduced-motion: reduce) {
  .suggest {
    animation: none;
  }
}

@media (max-width: 480px) {
  .suggest__list {
    max-height: max(
      88px,
      min(44vh, calc(100svh - var(--vv-bottom, 0px) - var(--chat-dock-h, 96px) - 96px))
    );
  }
  .suggest__foot {
    display: none;
  }
}
</style>
