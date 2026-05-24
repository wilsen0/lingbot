import type { KvNamespace } from "@/api/kv";

export interface AssetCard extends KvNamespace {
  id: string;
  label: string;
  kind: string;
  description: string;
  countLabel: string;
  priority: number;
}

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

const FISHING_ITEMS = new Set(["水桶价值", "鱼竿", "鱼饵"]);

function isPrivateTokenScope(scope: string): boolean {
  return /^啊\/(?:0|admin|cli-group|webui:.+)$/.test(scope);
}

export function describeAsset(ns: KvNamespace): AssetCard | null {
  const path = `${ns.scope}/${ns.file}`;
  if (ns.scope.startsWith("__")) return null;

  if (path === "啊/灵玉系/灵玉") {
    return {
      ...ns,
      id: path,
      label: "灵玉余额",
      kind: "货币",
      description: "可消费的个人余额",
      countLabel: `${ns.count} 人持有`,
      priority: 10,
    };
  }

  if (path === "fox/人性化ai/好感度记录") {
    return {
      ...ns,
      id: path,
      label: "好感度",
      kind: "关系",
      description: "你和该助手的互动值",
      countLabel: `${ns.count} 人持有`,
      priority: 15,
    };
  }

  if (ns.file === "禁言卡" && isPrivateTokenScope(ns.scope)) {
    return {
      ...ns,
      id: path,
      label: "禁言卡",
      kind: "卡券",
      description: "可用于禁言的道具",
      countLabel: `${ns.count} 人持有`,
      priority: 20,
    };
  }

  if (ns.scope === "休闲系/珍品" && TREASURE_ITEMS.has(ns.file)) {
    return {
      ...ns,
      id: path,
      label: ns.file,
      kind: ns.file === "个人守护" ? "守护" : "珍品",
      description: ns.file === "个人守护" ? "个人守护资格" : "背包内可持有物品",
      countLabel: `${ns.count} 人持有`,
      priority: 30,
    };
  }

  if (ns.scope === "休闲系/钓鱼" && FISHING_ITEMS.has(ns.file)) {
    return {
      ...ns,
      id: path,
      label: ns.file === "水桶价值" ? "水桶" : ns.file,
      kind: "钓鱼",
      description: ns.file === "水桶价值" ? "水桶收获价值" : "钓鱼玩法物品",
      countLabel: `${ns.count} 人持有`,
      priority: 40,
    };
  }

  return null;
}

export function toAssetCards(namespaces: KvNamespace[]): AssetCard[] {
  return namespaces
    .map(describeAsset)
    .filter((item): item is AssetCard => item !== null)
    .sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label, "zh-Hans-CN"));
}
