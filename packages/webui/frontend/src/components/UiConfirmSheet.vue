<template>
  <Teleport to="body">
    <Transition name="confirm">
      <div
        v-if="state.open"
        class="confirm-root"
        role="alertdialog"
        aria-modal="true"
        :aria-label="state.title"
      >
        <div class="confirm-veil" @click="cancel" />
        <div ref="panelEl" class="confirm-panel" tabindex="-1">
          <!-- 顶端一缕红线 + 一枚摇晃的铃，作为"焚断"动机 -->
          <svg class="confirm-thread" viewBox="0 0 200 70" preserveAspectRatio="none" aria-hidden="true">
            <path
              ref="threadPath"
              d="M0 8 C 50 24, 100 28, 100 50 C 100 28, 150 24, 200 8"
              stroke="rgb(var(--color-thread))"
              stroke-width="1.2"
              fill="none"
              stroke-linecap="round"
              :stroke-dasharray="threadLen"
              :stroke-dashoffset="dashOffset"
            />
            <circle cx="100" cy="58" r="3" fill="rgb(var(--color-sorrow))" />
          </svg>

          <h3 class="confirm-title font-display">{{ state.title }}</h3>
          <p v-if="state.hint" class="confirm-hint">{{ state.hint }}</p>

          <div class="confirm-actions">
            <button
              type="button"
              class="confirm-btn confirm-btn--ghost tap"
              @click="cancel"
            >
              <span class="font-display">{{ state.cancelLabel }}</span>
            </button>
            <button
              type="button"
              class="confirm-btn confirm-btn--primary tap"
              :class="`confirm-btn--${state.tone}`"
              @click="confirmIt"
            >
              <span class="font-display">{{ state.confirmLabel }}</span>
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import { useOverlay } from "@/composables/useOverlay";
import { _resolveConfirm, confirmState } from "@/composables/useConfirm";

const state = confirmState;

const panelEl = ref<HTMLElement | null>(null);
const threadPath = ref<SVGPathElement | null>(null);
const threadLen = ref(220);
const dashOffset = ref(220);

function cancel() {
  _resolveConfirm(false);
}
function confirmIt() {
  _resolveConfirm(true);
}

useOverlay(computed(() => state.open), panelEl, {
  dismissible: true,
  onClose: cancel,
});

watch(
  () => state.open,
  async (open) => {
    if (!open) return;
    await nextTick();
    if (threadPath.value) {
      const total = threadPath.value.getTotalLength();
      threadLen.value = total;
      dashOffset.value = total;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          dashOffset.value = 0;
        });
      });
    }
  },
);
</script>

<style scoped>
.confirm-root {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  /*
   * 输入法回正: 让 panel 居中于"键盘上方那块可见区".
   * padding-bottom = 键盘高度 → align-items: center 计算出的中点
   * 自然上移 vv-bottom/2 像素. 与 transform 等价但避开 iOS Safari
   * fixed+transform 的双重位移 quirk (见 ChatComposer / UiSheet 注释).
   */
  padding: 24px 24px calc(24px + var(--vv-bottom, 0px));
  transition: padding-bottom var(--dur-base) var(--ease-stand);
}
@media (prefers-reduced-motion: reduce) {
  .confirm-root { transition: none; }
}
.confirm-veil {
  position: absolute;
  inset: 0;
  background: rgb(0 0 0 / 0.55);
  backdrop-filter: blur(4px);
}
.confirm-panel {
  position: relative;
  width: 100%;
  max-width: 360px;
  padding: 22px 24px 20px;
  background: rgb(var(--color-bg-veil) / 0.96);
  backdrop-filter: blur(20px) saturate(140%);
  -webkit-backdrop-filter: blur(20px) saturate(140%);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .35),
    0 32px 80px rgb(0 0 0 / .55);
  outline: none;
}
.confirm-thread {
  width: 100%;
  height: 50px;
  margin: -12px 0 4px;
  pointer-events: none;
  filter: drop-shadow(0 0 6px rgb(var(--color-thread) / .35));
  transition: stroke-dashoffset var(--dur-stage) var(--ease-firm);
}
.confirm-title {
  text-align: center;
  font-size: 22px;
  letter-spacing: var(--track-poem);
  color: rgb(var(--color-ink));
  margin: 0 0 10px;
}
.confirm-hint {
  text-align: center;
  font-size: 13px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink-soft));
  line-height: 1.7;
  margin: 0 0 22px;
}
.confirm-actions {
  display: flex;
  gap: 12px;
  justify-content: space-between;
}
.confirm-btn {
  flex: 1;
  min-height: 48px;
  border: 0;
  cursor: pointer;
  font-family: var(--font-sans);
  font-size: 15px;
  letter-spacing: var(--track-poem);
  border-radius: var(--radius-seal);
  background: transparent;
  color: rgb(var(--color-ink));
  transition: transform var(--dur-tap) var(--ease-tap),
              color var(--dur-fast) ease,
              filter var(--dur-fast) ease,
              background var(--dur-base) ease;
}
.confirm-btn:active { transform: scale(0.97); }
.confirm-btn--ghost {
  background: rgb(var(--color-ink) / 0.05);
  color: rgb(var(--color-ink));
}
.confirm-btn--ghost:hover {
  background: rgb(var(--color-ink) / 0.1);
}
.confirm-btn--primary { color: rgb(var(--color-bg)); }
.confirm-btn--primary.confirm-btn--alert {
  background: rgb(var(--color-alert));
  box-shadow: 0 6px 20px rgb(var(--color-alert) / .42);
}
.confirm-btn--primary.confirm-btn--thread {
  background: linear-gradient(135deg, rgb(var(--color-sorrow)), rgb(var(--color-sakura-2)));
  box-shadow: 0 6px 20px rgb(var(--color-sorrow) / .35);
}
.confirm-btn--primary.confirm-btn--ash {
  background: rgb(var(--color-ash));
  box-shadow: 0 4px 14px rgb(0 0 0 / .35);
}
.confirm-btn--primary:hover { filter: brightness(1.08); }

.confirm-enter-active,
.confirm-leave-active {
  transition: opacity var(--dur-base) ease;
}
.confirm-enter-active .confirm-panel,
.confirm-leave-active .confirm-panel {
  transition: transform var(--dur-base) var(--ease-stand);
}
.confirm-enter-from { opacity: 0; }
.confirm-leave-to   { opacity: 0; }
.confirm-enter-from .confirm-panel { transform: translateY(20px) scale(0.96); }
.confirm-leave-to   .confirm-panel { transform: translateY(10px) scale(0.98); }

@media (prefers-reduced-motion: reduce) {
  .confirm-thread { transition: none; }
}
</style>
