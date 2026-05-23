import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h } from "vue";

import { createRafSchedule, useRafSchedule } from "@/composables/useRafSchedule";

/**
 * 守护:
 *   - trigger 同帧多次只跑一次
 *   - flush 立即跑且取消 RAF
 *   - cancel 取消不执行
 *   - pending 反映状态
 *   - 组件版本 unmount 时自动 cancel
 *
 * 用 stub RAF 让测试同步可控, 不依赖真实帧。
 */

describe("createRafSchedule", () => {
  let raf: ReturnType<typeof vi.fn>;
  let cancel: ReturnType<typeof vi.fn>;
  /** 模拟浏览器: id → callback. cancelAnimationFrame 删 key. flushFrames 跑所有还存在的。 */
  let pending: Map<number, () => void>;
  let nextId: number;

  beforeEach(() => {
    pending = new Map();
    nextId = 1;
    raf = vi.fn((cb: FrameRequestCallback) => {
      const id = nextId++;
      pending.set(id, () => cb(0));
      return id;
    });
    cancel = vi.fn((id: number) => {
      pending.delete(id);
    });
    vi.stubGlobal("requestAnimationFrame", raf);
    vi.stubGlobal("cancelAnimationFrame", cancel);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function flushFrames() {
    for (const fn of pending.values()) fn();
    pending.clear();
  }

  it("同帧多次 trigger 只调一次 RAF", () => {
    const work = vi.fn();
    const s = createRafSchedule(work);
    s.trigger();
    s.trigger();
    s.trigger();
    expect(raf).toHaveBeenCalledTimes(1);
    flushFrames();
    expect(work).toHaveBeenCalledTimes(1);
  });

  it("flush 立即跑 work 并取消 RAF", () => {
    const work = vi.fn();
    const s = createRafSchedule(work);
    s.trigger();
    s.flush();
    expect(work).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalled();
    flushFrames();
    // RAF 已取消, work 不应再次运行
    expect(work).toHaveBeenCalledTimes(1);
  });

  it("cancel 取消挂起, 不执行 work", () => {
    const work = vi.fn();
    const s = createRafSchedule(work);
    s.trigger();
    s.cancel();
    flushFrames();
    expect(work).not.toHaveBeenCalled();
  });

  it("pending 反映当前是否有挂起", () => {
    const s = createRafSchedule(() => undefined);
    expect(s.pending).toBe(false);
    s.trigger();
    expect(s.pending).toBe(true);
    s.flush();
    expect(s.pending).toBe(false);
  });

  it("flush 在没挂起时是 no-op (不重复执行)", () => {
    const work = vi.fn();
    const s = createRafSchedule(work);
    s.flush();
    s.flush();
    expect(work).not.toHaveBeenCalled();
  });
});

describe("useRafSchedule (component lifecycle)", () => {
  let cancel: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    cancel = vi.fn();
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", cancel);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("unmount 时自动 cancel 挂起", () => {
    const work = vi.fn();
    const Host = defineComponent({
      setup() {
        const sched = useRafSchedule(work);
        sched.trigger();
        return () => h("div");
      },
    });
    const w = mount(Host);
    expect(cancel).not.toHaveBeenCalled();
    w.unmount();
    expect(cancel).toHaveBeenCalled();
  });
});
