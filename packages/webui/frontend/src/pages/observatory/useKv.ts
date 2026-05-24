import { ref } from "vue";
import { isAxiosError } from "axios";

import {
  listNamespaces,
  publicRankKv,
  readKey,
  type KvNamespace,
  type KvPublicRankResponse,
} from "@/api/kv";

import type { AssetCard } from "./assetCatalog";

export interface OwnAssetValue {
  assetId: string;
  value: string | null;
  status: "held" | "empty" | "error";
}

export type PublicRankRow = KvPublicRankResponse["rows"][number];

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
  const ownValues = ref<Record<string, OwnAssetValue>>({});
  const loadingOwn = ref(false);
  const rankRows = ref<PublicRankRow[]>([]);
  const loadingRank = ref(false);
  const rankError = ref<string | null>(null);

  let ownSeq = 0;
  let rankSeq = 0;

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

  async function loadOwnAssets(assets: AssetCard[], ownerKey: string) {
    const readable = assets.filter((asset) => asset.ownReadable);
    const seq = ++ownSeq;
    if (!ownerKey || readable.length === 0) {
      ownValues.value = {};
      loadingOwn.value = false;
      return;
    }

    loadingOwn.value = true;
    const pairs = await Promise.all(
      readable.map(async (asset): Promise<[string, OwnAssetValue]> => {
        try {
          const { row } = await readKey({
            scope: asset.scope,
            file: asset.file,
            key: ownerKey,
          });
          return [asset.id, { assetId: asset.id, value: row.value, status: "held" }];
        } catch (error) {
          const statusCode = isAxiosError(error) ? error.response?.status : undefined;
          return [
            asset.id,
            {
              assetId: asset.id,
              value: null,
              status: statusCode === 404 ? "empty" : "error",
            },
          ];
        }
      }),
    );

    if (seq !== ownSeq) return;
    ownValues.value = Object.fromEntries(pairs);
    loadingOwn.value = false;
  }

  async function loadRank(asset: AssetCard, top = 12) {
    const seq = ++rankSeq;
    loadingRank.value = true;
    rankError.value = null;
    try {
      const response = await publicRankKv({
        scope: asset.scope,
        file: asset.file,
        order: "desc",
        top,
      });
      if (seq !== rankSeq) return;
      rankRows.value = response.rows;
    } catch {
      if (seq !== rankSeq) return;
      rankRows.value = [];
      rankError.value = "排行榜暂时不可用";
    } finally {
      if (seq === rankSeq) {
        loadingRank.value = false;
      }
    }
  }

  return {
    ns,
    loadingNs,
    ownValues,
    loadingOwn,
    rankRows,
    loadingRank,
    rankError,
    loadNs,
    loadOwnAssets,
    loadRank,
  };
}
