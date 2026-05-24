import { ref } from "vue";

import { searchAudit, type AuditEntry } from "@/api/audit";

/**
 * "系统记录" tab 的审计列表 store-like composable.
 *
 * 当前只有"按需 load 一次"的语义, 未做分页 / 滚动加载 — 那是后续话题。
 */
export function useAudit() {
  const rows = ref<AuditEntry[]>([]);
  const loading = ref(false);

  async function load() {
    loading.value = true;
    try {
      rows.value = await searchAudit({ limit: 120 });
    } catch {
      rows.value = [];
    } finally {
      loading.value = false;
    }
  }

  return { rows, loading, load };
}
