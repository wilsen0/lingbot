import { describe, expect, it } from "vitest";

import { describeAsset, toAssetCards } from "@/pages/observatory/assetCatalog";

function ns(scope: string, file: string, count = 1) {
  return { scope, file, count };
}

describe("asset catalog", () => {
  it("keeps only user-facing collection and leaderboard assets", () => {
    const cards = toAssetCards([
      ns("啊/灵玉系", "灵玉", 577),
      ns("啊/禁言系", "妖力", 243),
      ns("啊/节日系", "节日礼包", 126),
      ns("啊/活动系", "玫瑰花", 183),
      ns("休闲系/珍品", "小豆芽", 260),
      ns("休闲系/钓鱼", "鱼竿", 102),
      ns("fox/人性化ai", "好感度记录", 11),
      ns("__private", "internal", 99),
    ]);

    expect(cards.map((card) => card.label)).toEqual([
      "灵玉",
      "妖力",
      "节日礼包",
      "玫瑰花",
      "小豆芽",
    ]);
  });

  it("marks rank assets and hides internal ones", () => {
    const wealth = describeAsset(ns("啊/灵玉系", "灵玉", 577));
    const fishing = describeAsset(ns("休闲系/钓鱼", "鱼竿", 102));
    const affection = describeAsset(ns("fox/人性化ai", "好感度记录", 11));

    expect(wealth).toMatchObject({
      label: "灵玉",
      rankLabel: "财富榜",
      tabLabel: "灵玉",
      visibleInCollection: false,
      visibleInRank: true,
    });
    expect(fishing).toBeNull();
    expect(affection).toBeNull();
  });
});
