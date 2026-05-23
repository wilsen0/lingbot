import axios from "axios";
import { ref, watch } from "vue";

import {
  deleteKey,
  listKvKeys,
  listNamespaces,
  readKey,
  writeKey,
  type KvNamespace,
  type KvRow,
} from "@/api/kv";
import { confirmDestructive } from "@/composables/useConfirm";
import { toast, withToast } from "@/composables/useToast";

/**
 * KV 子系统的 store-like composable.
 *
 * 把"灵玉" tab 涉及的所有数据 + CRUD 编排独立出来:
 *   - 命名空间列表 ns (loadNs)
 *   - 当前选中的命名空间 currentNs (openNs)
 *   - 命名空间下的键值行 kvRows + 前缀过滤 kvPrefix (debounced)
 *   - 编辑 sheet 的 draft / etag / saving (openEdit / saveEdit / removeEdit)
 *
 * UI 侧 (ObsKvPane.vue) 只需要消费这些 ref + 调动作, 不持有 axios 调用。
 *
 * 设计取舍:
 *   • 错误处理统一走 toast (withToast/onError), 不再 try/catch 散落
 *   • watch(kvPrefix) 内联 debounce — 但不抽到全局: 这是 UI 侧的输入节流,
 *     不属于"业务编排"那一层
 *   • saveEdit 的 412 拦截留在 onError 钩子, 与"成功 toast"分开
 */
export function useKv() {
  const ns = ref<KvNamespace[]>([]);
  const loadingNs = ref(true);
  const currentNs = ref<KvNamespace | null>(null);

  const kvPrefix = ref("");
  const kvRows = ref<KvRow[]>([]);

  const editOpen = ref(false);
  const editDraft = ref<KvRow | null>(null);
  const editEtag = ref<string | null>(null);
  const saving = ref(false);

  let prefixDebounce: ReturnType<typeof setTimeout> | null = null;

  // ─────────── 加载命名空间列表 ───────────
  async function loadNs() {
    loadingNs.value = true;
    try {
      ns.value = await listNamespaces();
    } catch {
      // 阁中无玉 — 视为空集, 不弹 toast (loading 中态语义已足够)
      ns.value = [];
    } finally {
      loadingNs.value = false;
    }
  }

  // ─────────── 选中命名空间 ───────────
  async function openNs(n: KvNamespace) {
    currentNs.value = n;
    kvPrefix.value = "";
    await reloadKvRows();
  }

  async function reloadKvRows() {
    if (!currentNs.value) return;
    try {
      const r = await listKvKeys({
        scope: currentNs.value.scope,
        file: currentNs.value.file,
        prefix: kvPrefix.value || undefined,
        limit: 200,
      });
      kvRows.value = r.items;
    } catch {
      kvRows.value = [];
    }
  }

  watch(kvPrefix, () => {
    if (!currentNs.value) return;
    if (prefixDebounce) clearTimeout(prefixDebounce);
    prefixDebounce = setTimeout(() => {
      prefixDebounce = null;
      reloadKvRows();
    }, 260);
  });

  // ─────────── 编辑 sheet ───────────
  async function openEdit(r: KvRow) {
    await withToast("取玉失败", async () => {
      const got = await readKey({ scope: r.scope, file: r.file, key: r.key });
      editDraft.value = { ...got.row };
      editEtag.value = got.etag;
      editOpen.value = true;
    }).catch(() => {/* withToast 已 toast, 此处吞掉 rethrow 防止控制台噪声 */});
  }

  async function saveEdit() {
    if (!editDraft.value) return;
    saving.value = true;
    try {
      await withToast(
        "存玉失败",
        async () => {
          const draft = editDraft.value!;
          const { row: updated, etag: newEtag } = await writeKey({
            scope: draft.scope,
            file: draft.file,
            key: draft.key,
            value: draft.value,
            ifMatch: editEtag.value,
          });
          const idx = kvRows.value.findIndex((r) => r.key === updated.key);
          if (idx >= 0) kvRows.value[idx] = updated;
          editEtag.value = newEtag;
          editOpen.value = false;
          toast.success("已存", draft.key);
        },
        {
          // 412: 已被他人改写 — 给独立提示, 不当通用错误
          onError: (e) => {
            if (axios.isAxiosError(e) && e.response?.status === 412) {
              toast.warn("已被他人改写", "请重新打开");
              return true;
            }
            return false;
          },
        },
      );
    } catch {
      /* 失败已 toast, finally 收尾 */
    } finally {
      saving.value = false;
    }
  }

  async function removeEdit() {
    if (!editDraft.value) return;
    const draft = editDraft.value;
    const ok = await confirmDestructive(
      "焚此玉",
      `确认焚 ${draft.key}？焚后此键不复。`,
    );
    if (!ok) return;
    saving.value = true;
    try {
      await withToast("焚玉失败", async () => {
        await deleteKey({
          scope: draft.scope,
          file: draft.file,
          key: draft.key,
        });
        kvRows.value = kvRows.value.filter((r) => r.key !== draft.key);
        editOpen.value = false;
        toast.info("已焚", draft.key);
      });
    } catch {
      /* 失败已 toast */
    } finally {
      saving.value = false;
    }
  }

  return {
    // state
    ns,
    loadingNs,
    currentNs,
    kvPrefix,
    kvRows,
    editOpen,
    editDraft,
    saving,
    // actions
    loadNs,
    openNs,
    reloadKvRows,
    openEdit,
    saveEdit,
    removeEdit,
  };
}
