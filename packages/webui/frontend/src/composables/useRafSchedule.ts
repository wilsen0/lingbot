import { onBeforeUnmount } from "vue";

/**
 * 把"下一帧前合并执行一次"封装成一行可复用的 helper。
 *
 * ─────────── 设计动机 ───────────
 * 重构前在 6 处手写过同样的模式:
 *   let raf = 0;
 *   function trigger() {
 *     if (raf) return;
 *     raf = requestAnimationFrame(() => {
 *       raf = 0;
 *       work();
 *     });
 *   }
 *   onBeforeUnmount(() => { if (raf) cancelAnimationFrame(raf); });
 *
 * 6 处副本意味着 6 处都可能漏 cancel、漏处理 unmount。集中到这里, 保证:
 *
 *   - 同一帧内多次 trigger() 只跑一次 work()
 *   - flush() 强制立刻执行 (用于"先把待写入排空再做下一步"的语义)
 *   - cancel() 取消挂起的 work, 不执行
 *   - 组件 unmount 自动 cancel, 无内存泄漏
 *
 * 不绑组件实例时 (例如在 store / 全局 controller 里), 直接调
 * createRafSchedule() — 接口一致, 但不挂 onBeforeUnmount。
 */

export interface RafSchedule {
  /** 请求下一帧执行 work, 同帧多次 trigger 只跑一次。 */
  trigger(): void;
  /** 立即执行 (若有挂起), 否则 no-op。常用于"切换之前先 drain"。 */
  flush(): void;
  /** 取消挂起 (不执行)。 */
  cancel(): void;
  /** 当前是否有挂起。 */
  readonly pending: boolean;
}

/**
 * 不依赖组件实例的工厂版 — store / 模块顶层用。
 */
export function createRafSchedule(work: () => void): RafSchedule {
  let handle = 0;

  function trigger() {
    if (handle) return;
    handle = requestAnimationFrame(run);
  }
  function run() {
    handle = 0;
    work();
  }
  function flush() {
    if (!handle) return;
    cancelAnimationFrame(handle);
    handle = 0;
    work();
  }
  function cancel() {
    if (!handle) return;
    cancelAnimationFrame(handle);
    handle = 0;
  }

  return {
    trigger,
    flush,
    cancel,
    get pending() {
      return handle !== 0;
    },
  };
}

/**
 * 组件版本 — 自动在 unmount 时 cancel。setup() 里用就调这个。
 */
export function useRafSchedule(work: () => void): RafSchedule {
  const sched = createRafSchedule(work);
  onBeforeUnmount(() => sched.cancel());
  return sched;
}
