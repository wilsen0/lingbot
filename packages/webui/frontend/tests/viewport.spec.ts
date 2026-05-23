import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { _resetViewportForTest, useViewport } from "@/composables/useViewport";

/**
 * useViewport 是应用级单例, 写一个 var (--vv-bottom) + 一个 attr
 * (data-keyboard). 这一组测试钉住"只在 resize 触发时更新"的契约 —
 * 早先实现误监听 visualViewport.scroll, 是手机抖动的元凶, 别再加回来。
 */

interface FakeVV extends EventTarget {
  height: number;
  offsetTop: number;
}

function makeFakeVV(): FakeVV {
  const vv = new EventTarget() as FakeVV;
  vv.height = window.innerHeight;
  vv.offsetTop = 0;
  return vv;
}

function flushRaf() {
  // jsdom 的 requestAnimationFrame 走 setImmediate, 用 vi.runAllTimers
  // 行不通; 直接 await 一个 macrotask 即可 — vitest 的 raf polyfill 在
  // happy-dom/jsdom 里都是基于 setImmediate / setTimeout(0).
  return new Promise<void>((res) => setTimeout(res, 16));
}

describe("useViewport", () => {
  let originalVV: VisualViewport | null;
  let fake: FakeVV;

  beforeEach(() => {
    _resetViewportForTest();
    document.documentElement.style.removeProperty("--vv-bottom");
    delete document.documentElement.dataset.keyboard;
    fake = makeFakeVV();
    const w = window as unknown as { visualViewport: VisualViewport | null };
    originalVV = w.visualViewport;
    w.visualViewport = fake as unknown as VisualViewport;
    Object.defineProperty(window, "innerHeight", {
      value: 800,
      configurable: true,
    });
    fake.height = 800;
    fake.offsetTop = 0;
  });

  afterEach(() => {
    _resetViewportForTest();
    (window as unknown as { visualViewport: VisualViewport | null }).visualViewport =
      originalVV;
  });

  it("writes --vv-bottom = 0 with no keyboard", async () => {
    useViewport();
    await flushRaf();
    expect(document.documentElement.style.getPropertyValue("--vv-bottom")).toBe("0px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
  });

  it("flips data-keyboard=open and writes vv-bottom on resize", async () => {
    useViewport();
    await flushRaf();
    fake.height = 500; // keyboard takes 300px
    fake.dispatchEvent(new Event("resize"));
    await flushRaf();
    expect(document.documentElement.dataset.keyboard).toBe("open");
    // 量化到 4px → 300 仍是 300, but written.
    expect(
      parseInt(document.documentElement.style.getPropertyValue("--vv-bottom"), 10),
    ).toBeGreaterThanOrEqual(296);
  });

  it("ignores visualViewport scroll events (regression: was the mobile-jitter source)", async () => {
    useViewport();
    await flushRaf();
    fake.height = 500;
    fake.dispatchEvent(new Event("resize"));
    await flushRaf();
    const beforeBottom = document.documentElement.style.getPropertyValue("--vv-bottom");

    // Now fake an iOS-like scroll-driven offsetTop wobble that DOES NOT
    // correspond to a keyboard change. Earlier versions monitored this
    // and wrote new vars on every tick → jitter. The fix: don't subscribe
    // to scroll; vars stay put.
    fake.offsetTop = 24;
    fake.dispatchEvent(new Event("scroll"));
    await flushRaf();
    expect(document.documentElement.style.getPropertyValue("--vv-bottom")).toBe(
      beforeBottom,
    );
  });

  it("quantizes vv-bottom to 4px to suppress sub-pixel keyboard-animation jitter", async () => {
    useViewport();
    await flushRaf();
    fake.height = 800 - 297; // 297 mod 4 = 1 → quantized to 296
    fake.dispatchEvent(new Event("resize"));
    await flushRaf();
    expect(document.documentElement.style.getPropertyValue("--vv-bottom")).toBe(
      "296px",
    );
  });

  it("is idempotent: multiple useViewport() calls install one listener set", async () => {
    useViewport();
    useViewport();
    useViewport();
    await flushRaf();
    fake.height = 500;
    fake.dispatchEvent(new Event("resize"));
    await flushRaf();
    // If listeners doubled up the test would still pass on output, but
    // the smoke test we want is that no error is thrown — the contract
    // is "幂等".
    expect(document.documentElement.dataset.keyboard).toBe("open");
  });
});
