<template>
  <div class="obs-pane asset-stage" role="tabpanel">
    <UiEmptyState v-if="loadingNs" variant="compact">正在整理资产……</UiEmptyState>
    <UiEmptyState v-else-if="nsError" variant="compact">{{ nsError }}</UiEmptyState>
    <UiEmptyState v-else-if="assets.length === 0" variant="compact">暂无可展示资产</UiEmptyState>

    <div v-else class="asset-board">
      <section class="asset-panel asset-panel--mine" aria-labelledby="asset-mine-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">私藏</span>
            <h2 id="asset-mine-title" class="asset-panel__title">我的资产</h2>
          </span>
          <span class="asset-panel__metric font-mono">{{ mineSummary }}</span>
        </header>

        <details class="mine-group" open>
          <summary class="mine-group__summary">
            <span class="mine-group__title">已持有</span>
            <span class="mine-group__meta">{{ heldAssets.length }} 项</span>
            <span class="mine-group__chevron" aria-hidden="true">›</span>
          </summary>

          <UiEmptyState v-if="loadingOwn" variant="compact">正在读取资产……</UiEmptyState>
          <UiEmptyState v-else-if="heldAssets.length === 0" variant="compact">
            暂无已持有资产
          </UiEmptyState>

          <ul v-else class="mine-list" aria-label="已持有资产">
            <li
              v-for="asset in heldAssets"
              :key="asset.id"
              class="mine-item"
              :class="{
                'is-rank': asset.visibleInRank,
                'is-active': asset.id === selectedRankAssetId,
              }"
            >
              <span class="mine-item__mark font-display" aria-hidden="true">
                {{ assetBadge(asset) }}
              </span>
              <span class="mine-item__main">
                <span class="mine-item__name">{{ asset.label }}</span>
                <span class="mine-item__desc">{{ asset.description }}</span>
              </span>
              <span class="mine-item__value font-mono" :class="'is-' + ownAssetStatus(asset)">
                {{ ownAssetLabel(asset) }}
              </span>
            </li>
          </ul>
        </details>

        <details class="mine-group">
          <summary class="mine-group__summary">
            <span class="mine-group__title">其他资产</span>
            <span class="mine-group__meta">{{ otherAssets.length }} 项</span>
            <span class="mine-group__chevron" aria-hidden="true">›</span>
          </summary>

          <UiEmptyState v-if="otherAssets.length === 0" variant="compact">
            没有更多可读资产
          </UiEmptyState>

          <ul v-else class="mine-list mine-list--muted" aria-label="其他资产">
            <li
              v-for="asset in otherAssets"
              :key="asset.id"
              class="mine-item mine-item--muted"
            >
              <span class="mine-item__mark font-display" aria-hidden="true">
                {{ assetBadge(asset) }}
              </span>
              <span class="mine-item__main">
                <span class="mine-item__name">{{ asset.label }}</span>
                <span class="mine-item__desc">{{ asset.description }}</span>
              </span>
              <span class="mine-item__value font-mono" :class="'is-' + ownAssetStatus(asset)">
                {{ ownAssetLabel(asset) }}
              </span>
            </li>
          </ul>
        </details>
      </section>

      <section class="asset-panel asset-panel--stats" aria-labelledby="asset-stats-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">公开统计</span>
            <h2 id="asset-stats-title" class="asset-panel__title">收藏热度</h2>
          </span>
          <span class="asset-panel__metric font-mono">{{ collectionSummary }}</span>
        </header>

        <div class="collection-grid" aria-label="可统计的收藏资产">
          <article v-for="asset in collectionAssets" :key="asset.id" class="collection-card">
            <div class="collection-card__top">
              <span class="collection-card__mark font-display" aria-hidden="true">
                {{ assetBadge(asset) }}
              </span>
              <span class="collection-card__count font-mono">{{ asset.countLabel }}</span>
            </div>
            <div class="collection-card__body">
              <span class="collection-card__kind">{{ asset.kind }}</span>
              <h3 class="collection-card__label">{{ asset.label }}</h3>
              <p class="collection-card__desc">{{ asset.description }}</p>
            </div>
            <div class="collection-card__bar" aria-hidden="true">
              <span :style="{ width: collectionPercent(asset) + '%' }" />
            </div>
          </article>
        </div>
      </section>

      <section class="asset-panel asset-panel--rank" aria-labelledby="asset-rank-title">
        <header class="asset-panel__head">
          <span>
            <span class="asset-panel__eyebrow">匿名榜</span>
            <h2 id="asset-rank-title" class="asset-panel__title">
              {{ selectedRankAsset?.rankLabel ?? "排行榜" }}
            </h2>
          </span>
          <span class="asset-panel__metric">{{ selectedRankAsset?.tabLabel ?? "匿名展示" }}</span>
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
            :aria-label="asset.rankLabel ? `${asset.rankLabel} · ${asset.tabLabel ?? asset.label}` : asset.label"
            @click="selectedRankAssetId = asset.id"
          >
            <span class="rank-switch__primary">{{ asset.rankLabel ?? asset.label }}</span>
            <span class="rank-switch__secondary">{{ asset.tabLabel ?? asset.label }}</span>
          </button>
        </div>

        <UiEmptyState v-if="rankableAssets.length === 0" variant="compact">
          暂无可排行资产
        </UiEmptyState>
        <UiEmptyState v-else-if="loadingRank" variant="compact">正在读取排行……</UiEmptyState>
        <UiEmptyState v-else-if="rankError" variant="compact">{{ rankError }}</UiEmptyState>
        <UiEmptyState v-else-if="rankRows.length === 0" variant="compact">暂无排行数据</UiEmptyState>

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

import { toAssetCards, type AssetCard } from "./assetCatalog";
import { useKv } from "./useKv";

const kv = useKv();
const auth = useAuthStore();

const selectedRankAssetId = ref("");

const loadingNs = kv.loadingNs;
const nsError = kv.nsError;
const loadingOwn = kv.loadingOwn;
const loadingRank = kv.loadingRank;
const rankError = kv.rankError;
const rankRows = kv.rankRows;

const assets = computed(() => toAssetCards(kv.ns.value));
const ownerKey = computed(() => auth.profile?.sub ?? "");

const personalAssets = computed(() => assets.value.filter((asset) => asset.ownReadable));
const heldAssets = computed(() =>
  personalAssets.value.filter((asset) => kv.ownValues.value[asset.id]?.status === "held"),
);
const otherAssets = computed(() =>
  personalAssets.value.filter((asset) => kv.ownValues.value[asset.id]?.status !== "held"),
);
const collectionAssets = computed(() =>
  assets.value
    .filter((asset) => asset.visibleInCollection)
    .sort((a, b) => b.count - a.count || a.priority - b.priority || a.label.localeCompare(b.label, "zh-Hans-CN")),
);
const rankableAssets = computed(() => assets.value.filter((asset) => asset.visibleInRank));
const selectedRankAsset = computed(
  () => rankableAssets.value.find((asset) => asset.id === selectedRankAssetId.value) ?? null,
);

const collectionMaxCount = computed(() => Math.max(1, ...collectionAssets.value.map((asset) => asset.count)));

const mineSummary = computed(() => {
  if (!ownerKey.value) return "未登录";
  if (kv.loadingOwn.value) return "读取中";
  return `${heldAssets.value.length}/${personalAssets.value.length}`;
});

const collectionSummary = computed(() => `${collectionAssets.value.length} 种`);

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

function assetBadge(asset: AssetCard): string {
  return (asset.kind || asset.label).slice(0, 1);
}

function collectionPercent(asset: AssetCard): number {
  return Math.max(8, Math.round((asset.count / collectionMaxCount.value) * 100));
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
  if (asset.label === "个人守护" && String(own.value ?? "").trim() === "0") {
    return "未设定";
  }
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
  grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
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
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.78), rgb(var(--color-bg-veil) / 0.54));
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

.asset-panel--mine {
  grid-area: mine;
}

.asset-panel--stats {
  grid-area: stats;
}

.asset-panel--rank {
  grid-area: rank;
}

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
.leader-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.mine-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mine-list--muted {
  margin-top: 6px;
}

.mine-group {
  padding-top: 6px;
}

.mine-group + .mine-group {
  margin-top: 10px;
}

.mine-group__summary {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  cursor: pointer;
  list-style: none;
  color: rgb(var(--color-ink-soft));
  user-select: none;
}

.mine-group__summary::-webkit-details-marker {
  display: none;
}

.mine-group__title {
  color: rgb(var(--color-ink));
  font-size: 12px;
  font-weight: 620;
  letter-spacing: var(--track-fn);
}

.mine-group__meta {
  padding: 3px 7px;
  border: 1px solid rgb(var(--color-ink) / 0.06);
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.16);
  color: rgb(var(--color-ink-soft));
  font-size: 11px;
  letter-spacing: var(--track-fn);
}

.mine-group__chevron {
  margin-left: auto;
  color: rgb(var(--color-ink-soft));
  font-size: 18px;
  line-height: 1;
  transition: transform var(--dur-base) var(--ease-stand);
}

.mine-group[open] .mine-group__chevron {
  transform: rotate(90deg);
}

.mine-item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 8px 10px;
  border: 1px solid rgb(var(--color-ink) / 0.045);
  border-radius: 12px;
  background: rgb(var(--color-bg) / 0.13);
  transition:
    border-color var(--dur-base) ease,
    background var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap);
}

.mine-item.is-rank {
  border-color: rgb(var(--color-bell) / 0.14);
  background: linear-gradient(180deg, rgb(var(--color-bell) / 0.08), rgb(var(--color-bg) / 0.12));
}

.mine-item.is-active {
  border-color: rgb(var(--color-sorrow) / 0.22);
  background: linear-gradient(180deg, rgb(var(--color-sorrow) / 0.1), rgb(var(--color-bg) / 0.14));
}

.mine-item--muted {
  border-color: rgb(var(--color-ink) / 0.035);
  background: rgb(var(--color-bg) / 0.08);
  opacity: 0.88;
}

.mine-item__mark {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgb(var(--color-jade) / 0.18);
  border-radius: 9px 12px 9px 14px;
  background: linear-gradient(180deg, rgb(var(--color-jade) / 0.18), rgb(var(--color-bell) / 0.08));
  color: rgb(var(--color-jade));
  font-size: 15px;
  letter-spacing: 0;
}

.mine-item.is-rank .mine-item__mark {
  border-color: rgb(var(--color-bell) / 0.18);
  background: linear-gradient(180deg, rgb(var(--color-bell) / 0.2), rgb(var(--color-thread) / 0.1));
  color: rgb(var(--color-bell));
}

.mine-item.is-active .mine-item__mark {
  border-color: rgb(var(--color-sorrow) / 0.28);
  color: rgb(var(--color-sorrow));
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
  max-width: 132px;
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

.collection-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(172px, 1fr));
  gap: 10px;
}

.collection-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 132px;
  padding: 13px 14px 12px;
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: 12px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg) / 0.17), rgb(var(--color-bg-veil) / 0.18));
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / 0.05),
    0 10px 24px rgb(0 0 0 / 0.06);
}

.collection-card::before {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  top: 0;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-thread) / 0.25), transparent);
}

.collection-card__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.collection-card__mark {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgb(var(--color-bell) / 0.18);
  border-radius: 9px 11px 9px 12px;
  background: linear-gradient(180deg, rgb(var(--color-bell) / 0.16), rgb(var(--color-bg) / 0.12));
  color: rgb(var(--color-bell));
  font-size: 13px;
  letter-spacing: 0;
}

.collection-card__count {
  flex-shrink: 0;
  padding: 3px 7px;
  border: 1px solid rgb(var(--color-ink) / 0.06);
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.18);
  color: rgb(var(--color-ink-soft));
  font-size: 11px;
  letter-spacing: var(--track-fn);
}

.collection-card__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.collection-card__kind {
  color: rgb(var(--color-bell));
  font-size: 11px;
  letter-spacing: var(--track-fn);
  line-height: 1;
}

.collection-card__label {
  margin: 0;
  color: rgb(var(--color-ink));
  font-size: 14px;
  font-weight: 640;
  line-height: 1.18;
}

.collection-card__desc {
  margin: 0;
  color: rgb(var(--color-ink-soft));
  font-size: 11px;
  line-height: 1.45;
  letter-spacing: var(--track-meta);
}

.collection-card__bar {
  overflow: hidden;
  height: 6px;
  margin-top: auto;
  border-radius: 999px;
  background: rgb(var(--color-bg) / 0.24);
}

.collection-card__bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(to right, rgb(var(--color-thread) / 0.72), rgb(var(--color-bell) / 0.72));
  box-shadow: 0 0 12px rgb(var(--color-thread) / 0.18);
}

.rank-switch {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
  overflow-x: auto;
  scrollbar-width: none;
}

.rank-switch::-webkit-scrollbar {
  display: none;
}

.rank-switch__btn {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-height: 44px;
  min-width: 96px;
  padding: 8px 12px;
  border: 1px solid rgb(var(--color-ink) / 0.06);
  border-radius: 12px;
  background: rgb(var(--color-bg) / 0.18);
  color: rgb(var(--color-ink-soft));
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease,
    border-color var(--dur-fast) ease,
    transform var(--dur-tap) var(--ease-tap);
}

.rank-switch__btn:active {
  transform: scale(0.97);
}

.rank-switch__btn.is-active {
  border-color: rgb(var(--color-sorrow) / 0.22);
  background: linear-gradient(180deg, rgb(var(--color-sorrow) / 0.14), rgb(var(--color-sorrow) / 0.08));
  color: rgb(var(--color-sorrow));
}

.rank-switch__primary {
  font-size: 12px;
  font-weight: 620;
  line-height: 1.1;
}

.rank-switch__secondary {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  line-height: 1.1;
}

.rank-switch__btn.is-active .rank-switch__secondary {
  color: rgb(var(--color-sorrow) / 0.78);
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

  .leader-list {
    grid-template-columns: 1fr;
  }
}
</style>
