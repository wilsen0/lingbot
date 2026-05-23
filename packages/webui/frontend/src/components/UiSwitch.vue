<template>
  <button
    type="button"
    role="switch"
    :aria-checked="modelValue"
    :aria-label="label"
    class="ink-switch tap"
    :class="{ 'is-on': modelValue }"
    @click="toggle"
  >
    <!-- 一对铃，左：响（金光），右：静（墨色描边） -->
    <span class="ink-switch__track" aria-hidden="true">
      <svg class="ink-switch__bell" viewBox="0 0 24 28" fill="none">
        <path
          class="ink-switch__bell-body"
          d="M12 2 v 4 M 5 14 c 0 -4 3 -8 7 -8 s 7 4 7 8 v 4 l 2 2 h -18 l 2 -2 z"
          stroke="currentColor"
          stroke-width="1.4"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
        <circle class="ink-switch__bell-tongue" cx="12" cy="22" r="2" fill="currentColor" />
        <path
          class="ink-switch__bell-tassel"
          d="M9 22 q 3 6 0 5 M 15 22 q -3 6 0 5"
          stroke="currentColor"
          stroke-width="1.1"
          fill="none"
          stroke-linecap="round"
        />
      </svg>
      <span class="ink-switch__halo" />
    </span>
    <span class="ink-switch__label font-display">
      {{ modelValue ? onLabel : offLabel }}
    </span>
  </button>
</template>

<script setup lang="ts">
const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    onLabel?: string;
    offLabel?: string;
    label?: string;
  }>(),
  { onLabel: "响", offLabel: "静", label: "开关" },
);
const emit = defineEmits<(e: "update:modelValue", v: boolean) => void>();

function toggle() {
  emit("update:modelValue", !props.modelValue);
}
</script>

<style scoped>
.ink-switch {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px 6px 6px;
  background: transparent;
  border: 0;
  cursor: pointer;
  color: rgb(var(--color-ink-soft));
  border-radius: var(--radius-seal);
  transition:
    color var(--dur-base) ease,
    background var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap);
  min-height: 44px;
}
.ink-switch:hover {
  background: rgb(var(--color-ink) / 0.04);
}
.ink-switch:active {
  transform: scale(0.97);
}

.ink-switch__track {
  position: relative;
  display: inline-flex;
  width: 32px;
  height: 36px;
  align-items: center;
  justify-content: center;
}
.ink-switch__bell {
  width: 26px;
  height: 30px;
  position: relative;
  z-index: 1;
  transform-origin: 12px 4px;
  transition:
    color var(--dur-slow) ease,
    transform var(--dur-slow) var(--ease-swing);
}
.ink-switch.is-on .ink-switch__bell {
  color: rgb(var(--color-bell));
  animation: ink-switch-ring 480ms var(--ease-swing);
  filter: drop-shadow(0 0 6px rgb(var(--color-bell) / .55));
}
.ink-switch:not(.is-on) .ink-switch__bell {
  color: rgb(var(--color-ink-soft) / 0.65);
}
/* 静态时铃舌淡出（"无人时铃不响"） */
.ink-switch:not(.is-on) .ink-switch__bell-tongue,
.ink-switch:not(.is-on) .ink-switch__bell-tassel {
  opacity: 0.35;
}

.ink-switch__halo {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: radial-gradient(
    circle at 50% 50%,
    rgb(var(--color-bell) / 0.35) 0%,
    rgb(var(--color-bell) / 0) 70%
  );
  opacity: 0;
  transition: opacity var(--dur-slow) ease;
  pointer-events: none;
}
.ink-switch.is-on .ink-switch__halo {
  opacity: 1;
}

.ink-switch__label {
  font-size: 14px;
  letter-spacing: 0.32em;
  color: rgb(var(--color-ink));
  transition: color var(--dur-base) ease;
  min-width: 1em;
}
.ink-switch:not(.is-on) .ink-switch__label {
  color: rgb(var(--color-ink-soft));
}

@keyframes ink-switch-ring {
  0%   { transform: rotate(-18deg); }
  40%  { transform: rotate(14deg); }
  70%  { transform: rotate(-6deg); }
  100% { transform: rotate(0deg); }
}

@media (prefers-reduced-motion: reduce) {
  .ink-switch__bell,
  .ink-switch__halo {
    animation: none !important;
    transition: none !important;
  }
}
</style>
