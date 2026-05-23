import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick, ref } from "vue";

import { useElementSizeVar } from "@/composables/useElementSizeVar";

/**
 * 这些测试守护几个真实曾发生的债务:
 *
 * 1. unmount 时 ResizeObserver 必须被 disconnect (内存泄漏)
 * 2. unmount 时 :root 上的 css var 必须被清掉 (污染下一次进入)
 * 3. 退化路径 (无 ResizeObserver) 时 window.resize listener 也要 remove
 */

const Host = defineComponent({
  props: { cssVar: { type: String, required: true } },
  setup(props) {
    const el = ref<HTMLElement | null>(null);
    useElementSizeVar(el, props.cssVar, { kind: "height", min: 50 });
    return () => h("div", { ref: el, style: { height: "100px" } });
  },
});

describe("useElementSizeVar", () => {
  afterEach(() => {
    document.documentElement.style.cssText = "";
  });

  it("写入 css var 到 :root, 至少为 min", async () => {
    const wrapper = mount(Host, { props: { cssVar: "--test-h" } });
    // watch flush:'post' 在 mount 之后异步运行, 等一下再断言
    await nextTick();
    await flushPromises();
    // jsdom 里 offsetHeight 一般是 0, 应该被 min=50 提到 50px
    expect(document.documentElement.style.getPropertyValue("--test-h")).toBe("50px");
    wrapper.unmount();
  });

  it("unmount 时清掉 css var", async () => {
    const wrapper = mount(Host, { props: { cssVar: "--test-h-2" } });
    await nextTick();
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue("--test-h-2")).not.toBe("");
    wrapper.unmount();
    expect(document.documentElement.style.getPropertyValue("--test-h-2")).toBe("");
  });

  it("没有 ResizeObserver 也工作 (退化路径), unmount 时正确移除 window listener", async () => {
    const realRO = globalThis.ResizeObserver;
    // @ts-expect-error 临时拆掉
    delete globalThis.ResizeObserver;
    const removeSpy = vi.spyOn(window, "removeEventListener");
    try {
      const wrapper = mount(Host, { props: { cssVar: "--test-h-3" } });
      await nextTick();
      await flushPromises();
      expect(document.documentElement.style.getPropertyValue("--test-h-3")).toBe("50px");
      wrapper.unmount();
      const calledForResize = removeSpy.mock.calls.some(
        (args) => args[0] === "resize",
      );
      expect(calledForResize).toBe(true);
    } finally {
      removeSpy.mockRestore();
      globalThis.ResizeObserver = realRO;
    }
  });
});
