import type { KvNamespace } from "@/api/kv";

export interface AssetCard extends KvNamespace {
  id: string;
  label: string;
  tabLabel?: string;
  kind: string;
  description: string;
  countLabel: string;
  unit?: string;
  rankLabel?: string;
  visibleInCollection: boolean;
  visibleInRank: boolean;
  ownReadable: boolean;
  priority: number;
}

type AssetSpec = Omit<AssetCard, keyof KvNamespace>;

const RANK_PATHS = {
  wealth: "啊/灵玉系/灵玉",
  strength: "啊/禁言系/妖力",
} as const;

const TREASURE_ITEMS = new Set([
  "个人守护",
  "五彩棒",
  "呦呦",
  "哒咩",
  "大飞龙",
  "小白猫",
  "小豆芽",
  "思思",
  "气球",
  "蛋壳",
  "蛋壳壳",
  "郫忧",
]);

const GIFT_ITEMS = new Set(["节日礼包", "玫瑰花", "花标", "锦囊"]);

function buildAsset(ns: KvNamespace, spec: AssetSpec): AssetCard {
  return { ...ns, ...spec };
}

export function describeAsset(ns: KvNamespace): AssetCard | null {
  if (ns.scope.startsWith("__")) return null;

  const path = `${ns.scope}/${ns.file}`;

  if (path === RANK_PATHS.wealth) {
    return buildAsset(ns, {
      id: path,
      label: "灵玉",
      tabLabel: "灵玉",
      kind: "财富",
      description: "可公开排行的个人财富",
      countLabel: `${ns.count} 人持有`,
      unit: "灵玉",
      rankLabel: "财富榜",
      visibleInCollection: false,
      visibleInRank: true,
      ownReadable: true,
      priority: 10,
    });
  }

  if (path === RANK_PATHS.strength) {
    return buildAsset(ns, {
      id: path,
      label: "妖力",
      tabLabel: "妖力",
      kind: "实力",
      description: "可公开排行的实力数值",
      countLabel: `${ns.count} 人持有`,
      unit: "点",
      rankLabel: "实力榜",
      visibleInCollection: false,
      visibleInRank: true,
      ownReadable: true,
      priority: 15,
    });
  }

  if (path === "啊/节日系/节日礼包") {
    return buildAsset(ns, {
      id: path,
      label: "节日礼包",
      kind: "礼品",
      description: "节日活动礼品",
      countLabel: `${ns.count} 人持有`,
      visibleInCollection: true,
      visibleInRank: false,
      ownReadable: true,
      priority: 18,
    });
  }

  if (ns.scope === "啊/活动系" && GIFT_ITEMS.has(ns.file)) {
    return buildAsset(ns, {
      id: path,
      label: ns.file,
      kind: "礼品",
      description: "适合公开展示的活动礼品",
      countLabel: `${ns.count} 人持有`,
      visibleInCollection: true,
      visibleInRank: false,
      ownReadable: true,
      priority: ns.file === "花标" ? 27 : 24,
    });
  }

  if (ns.scope === "休闲系/珍品" && TREASURE_ITEMS.has(ns.file)) {
    return buildAsset(ns, {
      id: path,
      label: ns.file,
      kind: ns.file === "个人守护" ? "守护" : "珍品",
      description: ns.file === "个人守护" ? "当前守护对象" : "值得公开展示的珍品",
      countLabel: `${ns.count} 人持有`,
      unit: ns.file === "个人守护" ? undefined : "件",
      visibleInCollection: true,
      visibleInRank: false,
      ownReadable: true,
      priority: 30,
    });
  }

  return null;
}

export function toAssetCards(namespaces: KvNamespace[]): AssetCard[] {
  return namespaces
    .map(describeAsset)
    .filter((item): item is AssetCard => item !== null)
    .sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label, "zh-Hans-CN"));
}
