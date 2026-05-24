<template>
  <UiSheet
    :open="open"
    title="测试场景"
    subtitle="输入群号后，按该群规则运行对话"
    @update:open="$emit('update:open', $event)"
  >
    <form class="scope-form" @submit.prevent="onApply">
      <p class="scope-form__hint">
        留空 = 默认账号（避开主群规则）<br />
        填群号 = 按该群规则运行
      </p>
      <input
        v-model="draft"
        class="scope-form__input"
        type="text"
        inputmode="numeric"
        placeholder="例：754800438"
        autocomplete="off"
        spellcheck="false"
        aria-label="群号"
      />
      <div class="scope-form__btns">
        <button class="scope-form__btn tap" type="button" @click="onClear">清空</button>
        <button class="scope-form__btn scope-form__btn--primary tap" type="submit">应用</button>
      </div>
      <p v-if="current" class="scope-form__current">
        当前：<code>{{ current }}</code>
      </p>
    </form>
  </UiSheet>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";

import UiSheet from "@/components/UiSheet.vue";

const props = defineProps<{
  open: boolean;
  current: string;
}>();

const emit = defineEmits<{
  (e: "update:open", v: boolean): void;
  (e: "apply", value: string): void;
}>();

const draft = ref(props.current);

// 每次打开 sheet 时把 draft 同步到当前值, 关闭后保留 draft 不影响其他状态
watch(
  () => props.open,
  (v) => {
    if (v) draft.value = props.current;
  },
);

function onApply() {
  emit("apply", draft.value.trim());
}

function onClear() {
  draft.value = "";
  emit("apply", "");
}
</script>

<style scoped>
.scope-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 0 8px;
}
.scope-form__hint {
  font-size: 12px;
  letter-spacing: 0.06em;
  color: rgb(var(--color-ink-soft));
  line-height: 1.7;
  margin: 0;
}
.scope-form__input {
  font-family: var(--font-mono);
  font-size: 16px;
  padding: 11px 12px;
  background: linear-gradient(180deg, rgb(var(--color-bg) / 0.28), rgb(var(--color-bg) / 0.14));
  border: 1px solid rgb(var(--color-ink) / 0.1);
  border-radius: var(--radius-seal);
  color: rgb(var(--color-ink));
  outline: none;
  letter-spacing: 0.04em;
  transition:
    border-color var(--dur-fast) ease,
    background var(--dur-fast) ease;
}
.scope-form__input:focus {
  border-color: rgb(var(--color-thread));
  background: rgb(var(--color-bg) / 0.46);
}
.scope-form__btns {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.scope-form__btn {
  min-height: 44px;
  padding: 10px 18px;
  border-radius: var(--radius-seal);
  border: 1px solid rgb(var(--color-ink) / 0.18);
  background: rgb(var(--color-bg) / 0.16);
  color: rgb(var(--color-ink));
  font-family: var(--font-sans);
  font-size: 14px;
  letter-spacing: 0.08em;
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease,
    transform var(--dur-tap) var(--ease-tap);
}
.scope-form__btn:hover {
  background: rgb(var(--color-ink) / 0.06);
}
.scope-form__btn:active {
  transform: scale(0.96);
}
.scope-form__btn--primary {
  background: linear-gradient(180deg, rgb(var(--color-sorrow) / 0.18), rgb(var(--color-thread) / 0.12));
  color: rgb(var(--color-sorrow));
  border-color: rgb(var(--color-sorrow) / 0.35);
}
.scope-form__btn--primary:hover {
  background: rgb(var(--color-sorrow) / 0.22);
}
.scope-form__current {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  margin: 0;
}
.scope-form__current code {
  background: rgb(var(--color-ink) / 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
}
</style>
