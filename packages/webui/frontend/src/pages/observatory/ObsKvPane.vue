<template>
  <div class="obs-pane asset-stage" role="tabpanel">
    <UiEmptyState v-if="loadingNs" variant="compact">正在加载资产……</UiEmptyState>
    <UiEmptyState v-else-if="nsError" variant="compact">{{ nsError }}</UiEmptyState>
    <UiEmptyState v-else-if="assets.length === 0" variant="compact">暂无资产</UiEmptyState>

    <div v-else class="asset-board">
      <section class="asset-panel asset-panel--mine" aria-labelledby="asset-mine-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">私密</span>
            <h2 id="asset-mine-title" class="asset-panel__title">我的资产</h2>
          </span>
          <span class="asset-panel__metric font-mono">{{ mineSummary }}</span>
        </header>

        <ul class="mine-list" aria-label="我的资产">
          <li v-for="asset in personalAssets" :key="asset.id" class="mine-item">
            <span class="mine-item__mark font-display" aria-hidden="true">
              {{ asset.kind.slice(0, 1) }}
            </span>
            <span class="mine-item__main">
              <span class="mine-item__name">{{ asset.label }}</span>
              <span class="mine-item__desc">{{ asset.description }}</span>
            </span>
            <span
              class="mine-item__value font-mono"
              :class="'is-' + ownAssetStatus(asset)"
            >
              {{ ownAssetLabel(asset) }}
            </span>
          </li>
        </ul>
      </section>

      <section class="asset-panel asset-panel--stats" aria-labelledby="asset-stats-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">统计</span>
            <h2 id="asset-stats-title" class="asset-panel__title">持有人</h2>
          </span>
          <span class="asset-panel__metric font-mono">{{ holderSummary }}</span>
        </header>

        <div class="asset-filter" role="tablist" aria-label="资产分类">
          <button
            v-for="filter in statFilters"
            :key="filter.key"
            class="asset-filter__btn tap"
            :class="{ 'is-active': activeCategory === filter.key }"
            type="button"
            role="tab"
            :aria-selected="activeCategory === filter.key"
            @click="activeCategory = filter.key"
          >
            {{ filter.label }}
          </button>
        </div>

        <ul class="holder-list" aria-label="资产持有人统计">
          <li v-for="asset in statAssets" :key="asset.id">
            <button
              class="holder-row"
              :class="{ 'is-rankable': asset.rankable }"
              type="button"
              :disabled="!asset.rankable"
              @click="focusRank(asset)"
            >
              <span class="holder-row__main">
                <span class="holder-row__name">{{ asset.label }}</span>
                <span class="holder-row__kind">{{ asset.kind }}</span>
              </span>
              <span class="holder-row__bar" aria-hidden="true">
                <span :style="{ width: holderPercent(asset) + '%' }" />
              </span>
              <span class="holder-row__count font-mono">{{ asset.count }}</span>
            </button>
          </li>
        </ul>
      </section>

      <section class="asset-panel asset-panel--rank" aria-labelledby="asset-rank-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">匿名</span>
            <h2 id="asset-rank-title" class="asset-panel__title">排行榜</h2>
          </span>
          <span class="asset-panel__metric">不显示身份</span>
        </header>

        <div v-if="rankableAssets.length" class="rank-switch" role="tablist" aria-label="排行榜资产">
          <button
            v-for="asset in rankableAssets"
            :key="asset.id"
            class="rank-switch__btn tap"
            :class="{ 'is-active': selectedRankAssetId === asset.id }"
            type="button"
            role="tab"
            :aria-selected="selectedRankAssetId === asset.id"
            @click="selectedRankAssetId = asset.id"
          >
            {{ asset.label }}
          </button>
        </div>

        <UiEmptyState v-if="rankableAssets.length === 0" variant="compact">
          暂无可排行资产
        </UiEmptyState>
        <UiEmptyState v-else-if="loadingRank" variant="compact">正在读取排行……</UiEmptyState>
        <UiEmptyState v-else-if="rankError" variant="compact">{{ rankError }}</UiEmptyState>
        <UiEmptyState v-else-if="rankRows.length === 0" variant="compact">
          暂无排行数据
        </UiEmptyState>

        <ol v-else class="leader-list" aria-label="匿名排行榜">
          <li v-for="row in rankRows" :key="row.rank" class="leader-row">
            <span class="leader-row__rank font-mono">#{{ row.rank }}</span>
            <span class="leader-row__name">匿名持有人</span>
            <span class="leader-row__value font-mono">
              {{ formatAssetValue(row.value, selectedRankAsset?.unit) }}
            </span>
          </li>
        </ol>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, ref, watch } from "vue";

import UiEmptyState from "@/components/UiEmptyState.vue";
import { useAuthStore } from "@/store/auth";

import {
  ASSET_CATEGORY_LABELS,
  toAssetCards,
  type AssetCard,
  type AssetCategory,
} from "./assetCatalog";
import { useKv } from "./useKv";

type StatFilter = AssetCategory | "all";

const kv = useKv();
const auth = useAuthStore();

const loadingNs = kv.loadingNs;
const nsError = kv.nsError;
const loadingRank = kv.loadingRank;
const rankError = kv.rankError;
const rankRows = kv.rankRows;

const activeCategory = ref<StatFilter>("all");
const selectedRankAssetId = ref("");

const assets = computed(() => toAssetCards(kv.ns.value));
const ownerKey = computed(() => auth.profile?.sub ?? "");

const personalAssets = computed(() => assets.value.filter((asset) => asset.ownReadable));
const rankableAssets = computed(() => assets.value.filter((asset) => asset.rankable));
const selectedRankAsset = computed(
  () => rankableAssets.value.find((asset) => asset.id === selectedRankAssetId.value) ?? null,
);

const statFilters = computed<Array<{ key: StatFilter; label: string }>>(() => {
  const present = new Set(assets.value.map((asset) => asset.category));
  const filters: Array<{ key: StatFilter; label: string }> = [{ key: "all", label: "全部" }];
  for (const [key, label] of Object.entries(ASSET_CATEGORY_LABELS)) {
    if (present.has(key as AssetCategory)) {
      filters.push({ key: key as AssetCategory, label });
    }
  }
  return filters;
});

const statAssets = computed(() => {
  const list =
    activeCategory.value === "all"
      ? assets.value
      : assets.value.filter((asset) => asset.category === activeCategory.value);
  return [...list].sort((a, b) => b.count - a.count || a.priority - b.priority);
});

const maxHolderCount = computed(() =>
  Math.max(1, ...statAssets.value.map((asset) => asset.count)),
);

const mineSummary = computed(() => {
  if (!ownerKey.value) return "未登录";
  if (kv.loadingOwn.value) return "读取中";
  const held = personalAssets.value.filter(
    (asset) => kv.ownValues.value[asset.id]?.status === "held",
  ).length;
  return `${held}/${personalAssets.value.length}`;
});

const holderSummary = computed(() => {
  const count = statAssets.value.reduce((sum, asset) => sum + asset.count, 0);
  return `${count.toLocaleString("zh-CN")} 条`;
});

watch(
  [assets, ownerKey],
  ([nextAssets, nextOwner]) => {
    void kv.loadOwnAssets(nextAssets, nextOwner);
  },
  { immediate: true },
);

watch(
  () => auth.isAuthed,
  (authed) => {
    if (authed) {
      void kv.loadNs();
    }
  },
  { immediate: true },
);

watch(
  statFilters,
  (filters) => {
    if (!filters.some((filter) => filter.key === activeCategory.value)) {
      activeCategory.value = "all";
    }
  },
  { immediate: true },
);

watch(
  rankableAssets,
  (nextAssets) => {
    if (nextAssets.length === 0) {
      selectedRankAssetId.value = "";
      return;
    }
    if (!nextAssets.some((asset) => asset.id === selectedRankAssetId.value)) {
      selectedRankAssetId.value = nextAssets[0].id;
    }
  },
  { immediate: true },
);

watch(
  selectedRankAsset,
  (asset) => {
    if (asset) {
      void kv.loadRank(asset);
    }
  },
  { immediate: true },
);

onActivated(() => {
  if (auth.isAuthed) {
    void kv.loadNs();
  }
});

function focusRank(asset: AssetCard) {
  if (asset.rankable) {
    selectedRankAssetId.value = asset.id;
  }
}

function holderPercent(asset: AssetCard): number {
  return Math.max(8, Math.round((asset.count / maxHolderCount.value) * 100));
}

function ownAssetStatus(asset: AssetCard): "held" | "empty" | "error" | "loading" {
  const own = kv.ownValues.value[asset.id];
  if (!own && kv.loadingOwn.value) return "loading";
  return own?.status ?? "empty";
}

function ownAssetLabel(asset: AssetCard): string {
  const own = kv.ownValues.value[asset.id];
  if (!ownerKey.value) return "未登录";
  if (!own && kv.loadingOwn.value) return "读取中";
  if (!own || own.status === "empty") return "未持有";
  if (own.status === "error") return "读取失败";
  return formatAssetValue(own.value ?? "", asset.unit);
}

function formatAssetValue(raw: string | number, unit?: string): string {
  const value = String(raw).trim();
  const numeric = Number(value);
  const display =
    value !== "" && Number.isFinite(numeric)
      ? numeric.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
      : value || "0";
  return unit ? `${display} ${unit}` : display;
}
</script>

<style scoped>
.obs-pane {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
}

.asset-board {
  display: grid;
  grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
  grid-template-areas:
    "mine stats"
    "rank rank";
  gap: 12px;
}

.asset-panel {
  position: relative;
  overflow: hidden;
  padding: 16px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.78), rgb(var(--color-bg-veil) / 0.52));
  border: 1px solid rgb(var(--color-ink) / 0.055);
  border-radius: var(--radius-paper);
  color: rgb(var(--color-ink));
  backdrop-filter: blur(14px);
  box-shadow:
    0 1px 2px rgb(0 0 0 / 0.12),
    0 14px 32px rgb(0 0 0 / 0.12),
    inset 0 1px 0 rgb(255 255 255 / 0.07);
}

.asset-panel::before {
  content: "";
  position: absolute;
  left: 14px;
  right: 14px;
  top: 0;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-bell) / 0.36), transparent);
  pointer-events: none;
}

.asset-panel--mine { grid-area: mine; }
.asset-panel--stats { grid-area: stats; }
.asset-panel--rank { grid-area: rank; }

.asset-panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.asset-panel__eyebrow {
  display: block;
  margin-bottom: 3px;
  color: rgb(var(--color-bell));
  font-size: 11px;
  letter-spacing: var(--track-fn);
  line-height: 1;
}

.asset-panel__title {
  margin: 0;
  color: rgb(var(--color-ink));
  font-size: 18px;
  font-weight: 650;
  letter-spacing: 0;
  line-height: 1.15;
}

.asset-panel__metric {
  flex-shrink: 0;
  padding: 4px 8px;
  border: 1px solid rgb(var(--color-ink) / 0.06);
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.2);
  color: rgb(var(--color-ink-soft));
  font-size: 11px;
  letter-spacing: var(--track-fn);
  line-height: 1.3;
}

.mine-list,
.holder-list,
.leader-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.mine-list,
.holder-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mine-item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 52px;
}

.mine-item__mark {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgb(var(--color-jade) / 0.18);
  border-radius: 8px 12px 8px 14px;
  background: linear-gradient(180deg, rgb(var(--color-jade) / 0.18), rgb(var(--color-bell) / 0.08));
  color: rgb(var(--color-jade));
  font-size: 15px;
  letter-spacing: 0;
}

.mine-item__main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.mine-item__name {
  overflow: hidden;
  color: rgb(var(--color-ink));
  font-size: 14px;
  font-weight: 620;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mine-item__desc {
  overflow: hidden;
  color: rgb(var(--color-ink-soft));
  font-size: 11px;
  letter-spacing: var(--track-fn);
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mine-item__value {
  max-width: 116px;
  overflow: hidden;
  padding: 5px 8px;
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.22);
  color: rgb(var(--color-ink));
  font-size: 12px;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mine-item__value.is-empty,
.mine-item__value.is-loading {
  color: rgb(var(--color-ink-soft));
}

.mine-item__value.is-error {
  color: rgb(var(--color-alert));
}

.asset-filter,
.rank-switch {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
}

.asset-filter {
  margin-bottom: 12px;
}

.rank-switch {
  margin-bottom: 14px;
}

.asset-filter::-webkit-scrollbar,
.rank-switch::-webkit-scrollbar {
  display: none;
}

.asset-filter__btn,
.rank-switch__btn {
  flex: 0 0 auto;
  min-height: 34px;
  min-width: auto;
  padding: 0 11px;
  border: 1px solid rgb(var(--color-ink) / 0.06);
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.18);
  color: rgb(var(--color-ink-soft));
  font-size: 12px;
  letter-spacing: var(--track-fn);
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease,
    border-color var(--dur-fast) ease,
    transform var(--dur-tap) var(--ease-tap);
}

.asset-filter__btn:active,
.rank-switch__btn:active {
  transform: scale(0.96);
}

.asset-filter__btn.is-active,
.rank-switch__btn.is-active {
  border-color: rgb(var(--color-sorrow) / 0.22);
  background: rgb(var(--color-sorrow) / 0.12);
  color: rgb(var(--color-sorrow));
}

.holder-row {
  width: 100%;
  display: grid;
  grid-template-columns: minmax(92px, 1fr) minmax(80px, 0.9fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
}

.holder-row.is-rankable {
  cursor: pointer;
}

.holder-row:disabled {
  cursor: default;
}

.holder-row__main {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.holder-row__name {
  overflow: hidden;
  color: rgb(var(--color-ink));
  font-size: 13px;
  font-weight: 620;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.holder-row__kind {
  flex-shrink: 0;
  color: rgb(var(--color-ink-soft));
  font-size: 10px;
  letter-spacing: var(--track-fn);
}

.holder-row__bar {
  overflow: hidden;
  height: 7px;
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.24);
}

.holder-row__bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(to right, rgb(var(--color-thread) / 0.72), rgb(var(--color-bell) / 0.72));
  box-shadow: 0 0 12px rgb(var(--color-thread) / 0.18);
}

.holder-row__count {
  color: rgb(var(--color-ink-soft));
  font-size: 12px;
}

.leader-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
}

.leader-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 0 10px;
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.16);
}

.leader-row__rank {
  color: rgb(var(--color-bell));
  font-size: 12px;
}

.leader-row__name {
  overflow: hidden;
  color: rgb(var(--color-ink-soft));
  font-size: 12px;
  letter-spacing: var(--track-fn);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.leader-row__value {
  color: rgb(var(--color-ink));
  font-size: 12px;
  white-space: nowrap;
}

@media (max-width: 720px) {
  .asset-board {
    grid-template-columns: 1fr;
    grid-template-areas:
      "mine"
      "stats"
      "rank";
  }
}

@media (max-width: 460px) {
  .asset-panel {
    padding: 14px;
  }

  .mine-item {
    grid-template-columns: 32px minmax(0, 1fr);
  }

  .mine-item__value {
    grid-column: 2;
    justify-self: start;
    max-width: min(100%, 180px);
  }

  .holder-row {
    grid-template-columns: minmax(88px, 1fr) minmax(64px, 0.7fr) auto;
  }

  .leader-list {
    grid-template-columns: 1fr;
  }
}
</style>
