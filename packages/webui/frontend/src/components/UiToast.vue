<template>
  <Teleport to="body">
    <div class="ui-toast-stack" aria-live="polite" role="status">
      <Transition-group name="toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          class="ui-toast"
          :class="`ui-toast--${t.tone}`"
          @click="dismiss(t.id)"
        >
          <span class="ui-toast__dot" aria-hidden="true" />
          <div class="ui-toast__main">
            <p class="ui-toast__title font-serif">{{ t.title }}</p>
            <p v-if="t.body" class="ui-toast__body">{{ t.body }}</p>
          </div>
          <span class="ui-toast__seal" aria-hidden="true" />
        </div>
      </Transition-group>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { toasts, dismiss } from "@/composables/useToast";
</script>

<style scoped>
.ui-toast-stack {
  position: fixed;
  top: calc(env(safe-area-inset-top, 0) + 12px);
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  pointer-events: none;
  z-index: 80;
}
.ui-toast {
  position: relative;
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 260px;
  max-width: 86vw;
  padding: 12px 16px 12px 14px;
  background: rgb(var(--color-bg-veil) / 0.96);
  backdrop-filter: blur(14px);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .14),
    0 12px 32px rgb(var(--color-sorrow) / .14),
    inset 0 1px 0 rgb(255 255 255 / .08);
  cursor: pointer;
}
.ui-toast__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--color-thread));
  flex-shrink: 0;
}
.ui-toast__main { flex: 1; min-width: 0; }
.ui-toast__title {
  font-size: 14px;
  color: rgb(var(--color-ink));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
  letter-spacing: var(--track-fn);
}
.ui-toast__body {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 2px 0 0;
  letter-spacing: var(--track-fn);
}
/* 右下角一枚极小的朱印, 落款式 */
.ui-toast__seal {
  position: absolute;
  right: 6px;
  bottom: 6px;
  width: 6px;
  height: 6px;
  border-radius: 1px 3px 1px 4px;
  background: rgb(var(--color-sorrow) / 0.65);
  pointer-events: none;
}

.ui-toast--jade .ui-toast__dot {
  background: rgb(var(--color-jade));
}
.ui-toast--jade { box-shadow: 0 1px 2px rgb(0 0 0 / .14), 0 12px 32px rgb(var(--color-jade) / .14), inset 0 1px 0 rgb(255 255 255 / .08); }
.ui-toast--ash .ui-toast__dot {
  background: rgb(var(--color-ash));
}
.ui-toast--alert {
  background: rgb(var(--color-alert) / 0.16);
  box-shadow: 0 1px 2px rgb(0 0 0 / .18), 0 12px 32px rgb(var(--color-alert) / .2), inset 0 1px 0 rgb(255 255 255 / .08);
}
.ui-toast--alert .ui-toast__dot {
  background: rgb(var(--color-alert));
}
.ui-toast--bell .ui-toast__dot {
  background: rgb(var(--color-bell));
}

.toast-enter-active,
.toast-leave-active {
  transition: transform var(--dur-slow) var(--ease-stand), opacity var(--dur-slow) var(--ease-stand);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
