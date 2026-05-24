<template>
  <Teleport to="body">
    <Transition name="sheet">
      <div v-if="open" class="ui-sheet-root" role="dialog" aria-modal="true" :aria-label="title">
        <div class="ui-sheet-backdrop" @click="onBackdrop" />
        <div
          ref="panelEl"
          class="ui-sheet-panel"
          tabindex="-1"
          :style="{ '--drag-y': dragY + 'px' }"
          @touchstart="onDragStart"
          @touchmove="onDragMove"
          @touchend="onDragEnd"
          @touchcancel="onDragEnd"
        >
          <div class="ui-sheet-handle" aria-hidden="true" />
          <header v-if="title || $slots.header" class="ui-sheet-header">
            <slot name="header">
              <h3 class="font-display ui-sheet-title">{{ title }}</h3>
              <p v-if="subtitle" class="ui-sheet-subtitle">{{ subtitle }}</p>
            </slot>
          </header>
          <div class="ui-sheet-body"><slot /></div>
          <footer v-if="$slots.footer" class="ui-sheet-footer"><slot name="footer" /></footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, toRef } from "vue";

import { useOverlay } from "@/composables/useOverlay";

const props = withDefaults(
  defineProps<{
    open: boolean;
    title?: string;
    subtitle?: string;
    dismissible?: boolean;
  }>(),
  { dismissible: true, title: undefined, subtitle: undefined },
);
const emit = defineEmits<(e: "update:open", v: boolean) => void>();

const panelEl = ref<HTMLElement | null>(null);
const dragY = ref(0);
const dragStartY = ref<number | null>(null);

function close() {
  if (!props.dismissible) return;
  emit("update:open", false);
}

useOverlay(toRef(props, "open"), panelEl, {
  dismissible: props.dismissible,
  onClose: close,
});

function onBackdrop() {
  close();
}

function onDragStart(ev: TouchEvent) {
  if (!props.dismissible) return;
  dragStartY.value = ev.touches[0]?.clientY ?? null;
}

function onDragMove(ev: TouchEvent) {
  if (dragStartY.value == null) return;
  const delta = (ev.touches[0]?.clientY ?? 0) - dragStartY.value;
  dragY.value = Math.max(0, delta);
}

function onDragEnd() {
  if (dragStartY.value != null && dragY.value > 80) close();
  dragStartY.value = null;
  dragY.value = 0;
}
</script>

<style scoped>
.ui-sheet-root {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  /*
   * 输入法回正: 用 padding-bottom 让 flex-end 把 panel 推到键盘上沿,
   * 而不是 transform: translateY. transform 在 iOS Safari + position:fixed
   * 配合下会与浏览器自身的 scrollIntoView (聚焦时自动滚动焦点元素到中心)
   * 双重位移, 出现 ~80px+ 的空白. padding-bottom 是 layout property,
   * 浏览器在合成键盘动画时会把它正确地计算进 panel 的位置, 与 fixed
   * 元素一起"贴键盘上沿". 见 ChatComposer 同名注释.
   *
   * --vv-bottom 由 useViewport 写入, 默认 0px (无键盘).
   */
  padding-bottom: var(--vv-bottom, 0px);
  transition: padding-bottom var(--dur-base) var(--ease-stand);
}
@media (prefers-reduced-motion: reduce) {
  .ui-sheet-root { transition: none; }
}
.ui-sheet-backdrop {
  position: absolute;
  inset: 0;
  background: rgb(0 0 0 / 0.4);
  backdrop-filter: blur(3px);
}
.ui-sheet-panel {
  position: relative;
  width: 100%;
  max-width: 560px;
  /* 90% of viewport — svh 锁住"最小可见", 不会因为地址栏开合而变.
   * dvh 在 iOS 滚动时会重算, sheet 高度跟着抖. */
  max-height: 90svh;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.98) 0%, rgb(var(--color-bg-veil) / 0.92) 100%);
  backdrop-filter: blur(20px) saturate(140%);
  -webkit-backdrop-filter: blur(20px) saturate(140%);
  border-top: 1px solid rgb(var(--color-thread) / 0.18);
  border-left: 1px solid rgb(var(--color-thread) / 0.08);
  border-right: 1px solid rgb(var(--color-thread) / 0.08);
  border-top-left-radius: var(--radius-xl);
  border-top-right-radius: var(--radius-xl);
  padding: 10px 20px calc(env(safe-area-inset-bottom, 0) + 16px) 20px;
  box-shadow:
    0 -12px 36px rgb(0 0 0 / .34),
    0 1px 0 rgb(255 255 255 / .05) inset,
    0 18px 36px rgb(0 0 0 / .08);
  transform: translateY(var(--drag-y, 0));
  overflow-y: auto;
  overscroll-behavior: contain;
  outline: none;
}
.ui-sheet-handle {
  width: 44px;
  height: 4px;
  border-radius: 999px;
  background: linear-gradient(90deg, transparent, rgb(var(--color-thread) / 0.35), transparent);
  margin: 4px auto 12px;
}
.ui-sheet-header {
  margin-bottom: 12px;
}
.ui-sheet-title {
  font-size: 17px;
  letter-spacing: 0.18em;
  color: rgb(var(--color-ink));
  margin: 0;
}
.ui-sheet-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.08em;
}
.ui-sheet-footer {
  margin-top: 16px;
  padding-top: 12px;
  background: linear-gradient(to right, transparent, rgb(var(--color-thread) / 0.2), transparent) top / 100% 1px no-repeat;
}
.sheet-enter-active,
.sheet-leave-active {
  transition: opacity var(--dur-base) ease;
}
.sheet-enter-active .ui-sheet-panel,
.sheet-leave-active .ui-sheet-panel {
  transition: transform var(--dur-slow) var(--ease-stand);
}
.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}
.sheet-enter-from .ui-sheet-panel,
.sheet-leave-to .ui-sheet-panel {
  transform: translateY(100%);
}
@media (prefers-reduced-motion: reduce) {
  .sheet-enter-active,
  .sheet-leave-active {
    transition-duration: 0.001ms;
  }
}
</style>
