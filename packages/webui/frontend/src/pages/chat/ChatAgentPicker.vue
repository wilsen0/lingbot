<template>
  <UiSheet
    :open="open"
    title="择一红娘"
    subtitle="按一下名字即可换人"
    @update:open="$emit('update:open', $event)"
  >
    <ul v-if="agents.length" class="picker">
      <li v-for="a in agents" :key="a.name">
        <button
          class="picker__row tap"
          :class="{ 'is-active': a.name === currentName }"
          @click="$emit('pick', a.name)"
        >
          <span class="picker__mark" aria-hidden="true">·</span>
          <div class="picker__info">
            <p class="picker__name font-display">{{ a.name }}</p>
          </div>
          <span
            v-if="a.name === currentName"
            class="picker__current font-display"
            aria-hidden="true"
          >在</span>
        </button>
      </li>
    </ul>

    <UiEmptyState v-else variant="compact">
      <template #title>树上尚无红娘</template>
      在 <code>bot.yaml</code> 的 <code>agent.default_agent</code> 指向一份
      <code>agents/*.yaml</code> 即可。
    </UiEmptyState>
  </UiSheet>
</template>

<script setup lang="ts">
import type { AgentSummary } from "@/api/agents";
import UiEmptyState from "@/components/UiEmptyState.vue";
import UiSheet from "@/components/UiSheet.vue";

defineProps<{
  open: boolean;
  agents: AgentSummary[];
  currentName: string | null;
}>();

defineEmits<{
  (e: "update:open", v: boolean): void;
  (e: "pick", name: string): void;
}>();
</script>

<style scoped>
.picker {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}
.picker__row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 8px;
  background: transparent;
  border: 0;
  width: 100%;
  cursor: pointer;
  color: rgb(var(--color-ink));
  text-align: left;
  border-radius: var(--radius-seal);
  transition: color var(--dur-fast) ease,
              transform var(--dur-tap) var(--ease-tap),
              background var(--dur-fast) ease;
}
.picker__row:hover { background: rgb(var(--color-ink) / .04); }
.picker__row:active { transform: translateX(2px); }
.picker__row.is-active { color: rgb(var(--color-sorrow)); }
.picker__mark {
  font-size: 20px;
  color: rgb(var(--color-thread));
  line-height: 1;
}
.picker__info {
  flex: 1;
  min-width: 0;
}
.picker__name {
  font-size: 19px;
  letter-spacing: var(--track-poem);
}
.picker__meta {
  margin-top: 3px;
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}
.picker__current {
  font-size: 14px;
  letter-spacing: 0.18em;
  color: rgb(var(--color-sorrow));
  padding: 2px 10px;
  background: rgb(var(--color-sorrow) / .12);
  border-radius: var(--radius-seal);
}
code {
  background: rgb(var(--color-ink) / .06);
  padding: 1px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0;
}
</style>
