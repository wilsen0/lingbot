import { reactive, readonly } from "vue";

/**
 * Stage bus —  把"应用层有事发生"翻译成"背景里的铃响"。
 *
 * 任何组件都可以 `useStageBus().ringBell()`,
 * 由 `DecoSakuraTree` 监听 bellToken 后, 在 5 个铃位中选一个播放金光涟漪。
 *
 * 状态本身用一个单调递增的 token 触发, 避免重复值导致 watch 不再触发。
 *
 * 设计取舍 (历史): 早期还规划了 drawThread() 让背景红线绕一圈 — 但这条
 * 视觉始终未实现, 留下的死状态在重构时已删除。今后如要恢复, 在这里加
 * threadToken + 在 DecoSakuraTree 里加 watcher + SVG, 一并落实。
 */

interface StageState {
  /** 单调递增 token, 每次"撞铃"都+1, watcher 监听这个值。 */
  bellToken: number;
  /** 触发的铃槽 (0..4) — 由 DecoSakuraTree 的 BELL_ANCHORS 决定坐标。 */
  bellSlot: number;
}

const stage = reactive<StageState>({
  bellToken: 0,
  bellSlot: 0,
});

/** 静态计数器 — round-robin 5 槽。 */
const SLOT_COUNT = 5;
let lastSlot = 0;

export function useStageBus() {
  function ringBell(slot?: number): void {
    if (typeof slot === "number") {
      stage.bellSlot = ((slot % SLOT_COUNT) + SLOT_COUNT) % SLOT_COUNT;
    } else {
      lastSlot = (lastSlot + 1) % SLOT_COUNT;
      stage.bellSlot = lastSlot;
    }
    stage.bellToken += 1;
  }

  return {
    state: readonly(stage),
    ringBell,
  };
}
