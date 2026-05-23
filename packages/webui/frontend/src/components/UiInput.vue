<template>
  <label class="ink-input" :class="{ 'is-focused': focused, 'is-error': !!error }">
    <span v-if="label" class="ink-input__label">{{ label }}</span>
    <span class="ink-input__shell">
      <input
        :id="inputId"
        :value="modelValue"
        :type="type"
        :placeholder="placeholder"
        :autocomplete="autocomplete"
        :required="required"
        :disabled="disabled"
        :aria-label="label || placeholder"
        class="ink-input__field"
        @input="onInput"
        @focus="focused = true"
        @blur="focused = false"
      />
      <span class="ink-input__stroke" aria-hidden="true" />
    </span>
    <span v-if="error" class="ink-input__error" role="alert">{{ error }}</span>
    <span v-else-if="hint" class="ink-input__hint">{{ hint }}</span>
  </label>
</template>

<script setup lang="ts">
import { ref, useId } from "vue";

defineProps<{
  modelValue?: string;
  label?: string;
  placeholder?: string;
  type?: string;
  autocomplete?: string;
  hint?: string;
  error?: string;
  required?: boolean;
  disabled?: boolean;
}>();

const emit = defineEmits<(e: "update:modelValue", v: string) => void>();

const focused = ref(false);
const inputId = useId();

function onInput(ev: Event) {
  const target = ev.target as HTMLInputElement;
  emit("update:modelValue", target.value);
}
</script>

<style scoped>
/*
 * 笔墨输入框：无盒子边框，背景极淡，底部一笔"墨"（焦点时由苦情红替代）。
 */
.ink-input {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-family: var(--font-sans);
}
.ink-input__label {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.1em;
}
.ink-input__shell {
  position: relative;
  display: block;
}
.ink-input__field {
  width: 100%;
  min-height: 44px;
  padding: 10px 4px;
  background: transparent;
  border: 0;
  color: rgb(var(--color-ink));
  /*
   * 16px = iOS Safari "聚焦时不自动放大" 的下限. 低于 16px, 浏览器会
   * 把 layout viewport 强行放大到让此字段视觉等价 16px, 用户必须手动
   * 捏合还原 — 这是移动端最常见的"输入框被顶乱"成因. 全站可输入
   * 字段统一 ≥ 16px, 不再写 user-scalable=no (那是无障碍倒退).
   */
  font-size: 16px;
  outline: none;
  caret-color: rgb(var(--color-thread));
}
.ink-input__field::placeholder {
  color: rgb(var(--color-ink-soft) / 0.6);
}
/* "墨"的基底：淡灰一横；focus 时由红线替换并加粗。 */
.ink-input__shell::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 1px;
  background: rgb(var(--color-ink) / 0.18);
}
.ink-input__stroke {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: linear-gradient(
    to right,
    transparent 0%,
    rgb(var(--color-thread)) 10%,
    rgb(var(--color-sorrow)) 50%,
    rgb(var(--color-thread)) 90%,
    transparent 100%
  );
  transform: scaleX(0);
  transform-origin: center;
  transition: transform var(--dur-slow) var(--ease-firm);
  filter: drop-shadow(0 0 4px rgb(var(--color-thread) / 0.35));
}
.is-focused .ink-input__stroke {
  transform: scaleX(1);
}
.ink-input__hint {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
}
.ink-input__error {
  font-size: 11px;
  color: rgb(var(--color-alert));
}
.is-error .ink-input__shell::before {
  background: rgb(var(--color-alert));
}
</style>
