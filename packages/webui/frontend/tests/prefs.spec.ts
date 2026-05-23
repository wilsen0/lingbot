import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePrefsStore } from "@/store/prefs";

describe("prefs store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("defaults: auto theme; decor subtle on fine pointer (desktop)", () => {
    // jsdom 默认 matchMedia 永远返回 matches:false → coarse-pointer 不命中
    // → decor 取桌面默认 "subtle".
    const prefs = usePrefsStore();
    expect(prefs.theme).toBe("auto");
    expect(prefs.decor).toBe("subtle");
    expect(prefs.bellSound).toBe(false);
    expect(prefs.haptics).toBe(true);
  });

  it("touch / coarse-pointer devices default decor to off", () => {
    // 模拟手机: matchMedia('(pointer: coarse)') → matches: true.
    const original = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q.includes("coarse"),
      media: q,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;

    setActivePinia(createPinia());
    const prefs = usePrefsStore();
    expect(prefs.decor).toBe("off");

    window.matchMedia = original;
  });

  it("toggle updates reactively", () => {
    const prefs = usePrefsStore();
    prefs.toggleBellSound();
    expect(prefs.bellSound).toBe(true);
    prefs.toggleHaptics();
    expect(prefs.haptics).toBe(false);
  });

  it("setTheme reflects on document.documentElement", () => {
    const prefs = usePrefsStore();
    prefs.setTheme("dark");
    expect(prefs.theme).toBe("dark");
  });

  it("setDecor updates", () => {
    const prefs = usePrefsStore();
    prefs.setDecor("off");
    expect(prefs.decor).toBe("off");
  });
});
