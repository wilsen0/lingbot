<template>
  <div class="obs-pane" role="tabpanel">
    <UiEmptyState v-if="kv.loadingNs.value" variant="compact"> 正在加载资产…… </UiEmptyState>
    <UiEmptyState v-else-if="assets.length === 0" variant="compact"> 暂无资产 </UiEmptyState>

    <ul v-else class="assets" aria-label="资产">
      <li v-for="asset in assets" :key="asset.id">
        <article class="asset">
          <span class="asset__seal font-display" aria-hidden="true">
            {{ asset.kind.slice(0, 1) }}
          </span>
          <span class="asset__main">
            <span class="asset__head">
              <span class="asset__name font-display">{{ asset.label }}</span>
              <span class="asset__kind">{{ asset.kind }}</span>
            </span>
            <span class="asset__desc">{{ asset.description }}</span>
          </span>
          <span class="asset__count font-mono">{{ asset.countLabel }}</span>
        </article>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from "vue";

import UiEmptyState from "@/components/UiEmptyState.vue";

import { toAssetCards } from "./assetCatalog";
import { useKv } from "./useKv";

const kv = useKv();
const assets = computed(() => toAssetCards(kv.ns.value));

onMounted(() => {
  kv.loadNs();
});
</script>

<style scoped>
.obs-pane {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
}

.assets {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
  list-style: none;
  margin: 0;
  padding: 0;
}

.asset {
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 88px;
  padding: 15px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.82), rgb(var(--color-bg-veil) / 0.56));
  border: 1px solid rgb(var(--color-ink) / 0.055);
  border-radius: var(--radius-paper);
  color: rgb(var(--color-ink));
  backdrop-filter: blur(12px);
  box-shadow:
    0 1px 2px rgb(0 0 0 / 0.12),
    0 14px 30px rgb(0 0 0 / 0.13),
    inset 0 1px 0 rgb(255 255 255 / 0.08);
}
.asset::after {
  content: "";
  position: absolute;
  left: 18%;
  right: 18%;
  top: 0;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-bell) / 0.38), transparent);
  pointer-events: none;
}

.asset__seal {
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background:
    linear-gradient(180deg, rgb(var(--color-jade) / 0.2), rgb(var(--color-bell) / 0.12));
  color: rgb(var(--color-jade));
  border-radius: 4px 8px 4px 10px;
  border: 1px solid rgb(var(--color-jade) / 0.18);
  font-size: 17px;
  letter-spacing: 0;
  flex-shrink: 0;
  transform: rotate(-3deg);
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / 0.08),
    0 8px 18px rgb(var(--color-jade) / 0.08);
}

.asset__main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1;
}

.asset__head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.asset__name {
  flex: 1;
  font-size: 17px;
  letter-spacing: 0.1em;
  line-height: 1.2;
  color: rgb(var(--color-ink));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.asset__kind {
  flex-shrink: 0;
  padding: 2px 7px;
  border-radius: 999px;
  color: rgb(var(--color-bell));
  background: rgb(var(--color-bell) / 0.12);
  border: 1px solid rgb(var(--color-bell) / 0.12);
  font-size: 10px;
  letter-spacing: 0.06em;
  line-height: 1.4;
}

.asset__desc {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.06em;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset__count {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: 0.04em;
  flex-shrink: 0;
  font-feature-settings: "tnum" on;
  padding: 4px 8px;
  background: rgb(var(--color-bg) / 0.22);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 999px;
  align-self: flex-start;
}

@media (max-width: 420px) {
  .assets {
    grid-template-columns: 1fr;
  }
  .asset {
    align-items: flex-start;
  }
}
</style>
