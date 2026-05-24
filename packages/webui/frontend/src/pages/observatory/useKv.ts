import { ref } from "vue";

import { listNamespaces, type KvNamespace } from "@/api/kv";

/**
 * Asset inventory data source.
 *
 * The backend still exposes raw KV for admin tools, but the observatory
 * page only needs the namespace directory. `assetCatalog` then decides
 * which namespaces are user-facing assets and which are internal state.
 */
export function useKv() {
  const ns = ref<KvNamespace[]>([]);
  const loadingNs = ref(true);

  async function loadNs() {
    loadingNs.value = true;
    try {
      ns.value = await listNamespaces();
    } catch {
      ns.value = [];
    } finally {
      loadingNs.value = false;
    }
  }

  return {
    ns,
    loadingNs,
    loadNs,
  };
}
