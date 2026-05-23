<template>
  <div class="obs-pane" role="tabpanel">
    <div v-if="kv.currentNs.value" class="crumbs">
      <button class="link tap" @click="kv.currentNs.value = null">
        <span aria-hidden="true">←</span>
        <span class="font-display">回</span>
      </button>
      <span class="crumbs__path font-mono">
        {{ kv.currentNs.value.scope }} / {{ kv.currentNs.value.file }}
      </span>
    </div>

    <UiEmptyState v-if="kv.loadingNs.value && !kv.currentNs.value" variant="compact">
      阁中取玉……
    </UiEmptyState>
    <UiEmptyState
      v-else-if="!kv.currentNs.value && kv.ns.value.length === 0"
      variant="compact"
    >
      阁中尚无玉
    </UiEmptyState>

    <!-- 命名空间 · 玉牌网格 -->
    <ul v-else-if="!kv.currentNs.value" class="plaques">
      <li v-for="n in kv.ns.value" :key="n.scope + '/' + n.file">
        <button class="plaque tap" @click="kv.openNs(n)">
          <span class="plaque__seal" aria-hidden="true">玉</span>
          <span class="plaque__main">
            <span class="plaque__scope font-display">{{ n.scope }}</span>
            <span class="plaque__file font-mono">{{ n.file }}</span>
          </span>
          <span class="plaque__count font-mono">{{ n.count }}</span>
        </button>
      </li>
    </ul>

    <div v-else>
      <div class="search-row">
        <input
          v-model="kv.kvPrefix.value"
          placeholder="寻玉 · 前缀"
          class="search"
          aria-label="按前缀搜索"
        />
      </div>
      <ul class="rows">
        <li v-for="r in kv.kvRows.value" :key="r.key">
          <button class="row tap" @click="kv.openEdit(r)">
            <span class="row__key font-mono">{{ r.key }}</span>
            <span class="row__value font-mono">{{ r.value }}</span>
          </button>
        </li>
        <li v-if="!kv.kvRows.value.length">
          <UiEmptyState variant="compact">无匹配</UiEmptyState>
        </li>
      </ul>
    </div>

    <!-- 编辑 sheet · 主操作上方 / 危险动作下方且独立 -->
    <UiSheet
      v-model:open="kv.editOpen.value"
      title="修玉"
      :subtitle="auth.canWrite ? '修后即录入册' : '只读 — 仅供查阅'"
    >
      <div v-if="kv.editDraft.value" class="edit">
        <p class="edit__path font-mono">
          {{ kv.editDraft.value.scope }} / {{ kv.editDraft.value.file }} /
          {{ kv.editDraft.value.key }}
        </p>
        <textarea
          v-model="kv.editDraft.value.value"
          class="edit__area"
          :readonly="!auth.canWrite"
          rows="6"
          aria-label="玉值"
        />
        <p v-if="!auth.canWrite" class="edit__readonly">
          此账号为只读 · 无法修玉
        </p>

        <div v-if="auth.canWrite" class="edit__actions">
          <UiButton
            kind="primary"
            :loading="kv.saving.value"
            :disabled="kv.saving.value"
            @click="kv.saveEdit"
          >
            存
          </UiButton>
          <UiButton
            kind="ghost"
            :disabled="kv.saving.value"
            @click="kv.editOpen.value = false"
          >
            回
          </UiButton>
        </div>
        <div v-else class="edit__actions">
          <UiButton kind="ghost" @click="kv.editOpen.value = false">回</UiButton>
        </div>

        <div v-if="auth.canWrite" class="edit__danger">
          <button
            type="button"
            class="edit__remove tap"
            :disabled="kv.saving.value"
            @click="kv.removeEdit"
          >
            <span class="edit__remove-mark" aria-hidden="true">·</span>
            <span class="font-display">焚此玉</span>
          </button>
          <p class="edit__remove-hint">删除此键值对，不可还原。</p>
        </div>
      </div>
    </UiSheet>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from "vue";

import UiButton from "@/components/UiButton.vue";
import UiEmptyState from "@/components/UiEmptyState.vue";
import UiSheet from "@/components/UiSheet.vue";
import { useAuthStore } from "@/store/auth";

import { useKv } from "./useKv";

const auth = useAuthStore();
const kv = useKv();

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

/* ────── 玉牌网格 ────── */
.plaques {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  list-style: none;
  margin: 0;
  padding: 0;
}
.plaque {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 14px 14px;
  background: rgb(var(--color-bg-veil) / .58);
  border: 0;
  cursor: pointer;
  border-radius: var(--radius-paper);
  text-align: left;
  color: rgb(var(--color-ink));
  transition:
    background var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap),
    box-shadow var(--dur-base) ease,
    color var(--dur-base) ease;
  min-height: 64px;
  backdrop-filter: blur(12px);
  box-shadow: 0 1px 2px rgb(0 0 0 / .12), 0 6px 18px rgb(0 0 0 / .14);
}
.plaque:hover {
  background: rgb(var(--color-bg-veil) / .82);
  box-shadow: 0 1px 2px rgb(0 0 0 / .14), 0 12px 28px rgb(var(--color-jade) / .14);
  color: rgb(var(--color-sorrow));
}
.plaque:active { transform: scale(0.98); }

.plaque__seal {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: rgb(var(--color-jade) / .18);
  color: rgb(var(--color-jade));
  border-radius: 4px 8px 4px 10px;
  font-family: var(--font-display);
  font-size: 16px;
  letter-spacing: 0;
  flex-shrink: 0;
  transform: rotate(-3deg);
}
.plaque__main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.plaque__scope {
  font-size: 16px;
  letter-spacing: 0.18em;
  line-height: 1.2;
  color: inherit;
}
.plaque__file {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plaque__count {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
  flex-shrink: 0;
  font-feature-settings: "tnum" on;
}

/* ────── 面包屑 ────── */
.crumbs {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 4px;
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: transparent;
  border: 0;
  padding: 8px 12px;
  color: rgb(var(--color-ink-soft));
  cursor: pointer;
  letter-spacing: 0.2em;
  border-radius: var(--radius-seal);
  transition: color var(--dur-fast) ease, background var(--dur-base) ease;
}
.link:hover {
  color: rgb(var(--color-ink));
  background: rgb(var(--color-ink) / .04);
}
.crumbs__path {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}

/* ────── 搜索 ────── */
.search-row { margin-bottom: 10px; }
.search {
  width: 100%;
  min-height: 44px;
  background: rgb(var(--color-bg-veil) / 0.6);
  border: 0;
  outline: none;
  padding: 8px 16px;
  border-radius: var(--radius-paper);
  color: rgb(var(--color-ink));
  /* iOS 聚焦时不自动放大 — 见 UiInput / ChatComposer 同名注释 */
  font-size: 16px;
  font-family: var(--font-display);
  letter-spacing: 0.15em;
  transition: background var(--dur-fast) ease, box-shadow var(--dur-fast) ease;
}
.search::placeholder {
  color: rgb(var(--color-ink-soft) / 0.65);
  letter-spacing: 0.3em;
}
.search:focus {
  background: rgb(var(--color-bg-veil) / 0.85);
  box-shadow: 0 0 0 3px rgb(var(--color-thread) / 0.25);
}

/* ────── 行 ────── */
.rows {
  display: flex;
  flex-direction: column;
  list-style: none;
  margin: 0;
  padding: 0;
}
.row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 12px;
  min-height: 44px;
  background: transparent;
  border: 0;
  cursor: pointer;
  width: 100%;
  text-align: left;
  position: relative;
  transition: background var(--dur-fast) ease;
  border-radius: var(--radius-seal);
}
.rows > li + li > .row::before {
  content: "";
  position: absolute;
  top: 0;
  left: 12%;
  right: 12%;
  height: 1px;
  background: linear-gradient(
    to right,
    transparent,
    rgb(var(--color-thread) / 0.18),
    transparent
  );
}
.row:hover { background: rgb(var(--color-ink) / 0.04); }
.row__key {
  font-size: 14px;
  color: rgb(var(--color-ink));
  max-width: 48%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row__value {
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  max-width: 48%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
}

/* ────── sheet edit ────── */
.edit__path {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  margin-bottom: 10px;
  word-break: break-all;
  letter-spacing: var(--track-fn);
}
.edit__area {
  width: 100%;
  min-height: 160px;
  background: rgb(var(--color-bg) / 0.6);
  border: 0;
  outline: none;
  border-radius: var(--radius-paper);
  padding: 12px 14px;
  font-family: var(--font-mono);
  /* iOS 聚焦时不自动放大 — 见 UiInput / ChatComposer 同名注释 */
  font-size: 16px;
  line-height: 1.6;
  color: rgb(var(--color-ink));
  resize: vertical;
  transition: box-shadow var(--dur-fast) ease;
}
.edit__area:focus {
  box-shadow: 0 0 0 3px rgb(var(--color-thread) / 0.3);
}
.edit__actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}
.edit__readonly {
  margin: 8px 0 0;
  font-size: 11px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink-soft));
  text-align: right;
}

/* 危险动作独立成一行 — 用墨字 + 朱点, 不和"存"在视觉上比邻 */
.edit__danger {
  margin-top: 28px;
  padding-top: 16px;
  background: linear-gradient(
    to right,
    transparent,
    rgb(var(--color-alert) / 0.22),
    transparent
  ) top / 100% 1px no-repeat;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.edit__remove {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 0;
  cursor: pointer;
  color: rgb(var(--color-alert));
  padding: 8px 14px;
  border-radius: var(--radius-seal);
  font-size: 14px;
  letter-spacing: 0.22em;
  min-height: 44px;
  transition: color var(--dur-fast) ease, background var(--dur-fast) ease, transform var(--dur-tap) var(--ease-tap);
}
.edit__remove:hover { background: rgb(var(--color-alert) / .08); }
.edit__remove:active { transform: scale(0.96); }
.edit__remove:disabled { opacity: 0.5; cursor: not-allowed; }
.edit__remove-mark {
  font-size: 18px;
  line-height: 1;
}
.edit__remove-hint {
  font-size: 10px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}
</style>
