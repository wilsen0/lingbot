import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it } from "vitest";
import { defineComponent, h, nextTick, ref } from "vue";

import { useOverlay } from "@/composables/useOverlay";

/**
 * 守护 overlay lifecycle 的几个真实债务:
 *
 * 1. 嵌套 overlay 同时 Esc → 仅栈顶 close (用 keydown capture 拦截)
 * 2. body 锁多 overlay 叠开计数, 最后一个关掉才真正 unlock
 * 3. unmount 时若 overlay 仍 open, 必须从栈里移除 (否则后续 Esc 路由错位)
 */

function makeHost(initialOpen = false) {
  const closes: number[] = [];
  const open = ref(initialOpen);
  const Host = defineComponent({
    setup(_, { expose }) {
      const panel = ref<HTMLElement | null>(null);
      useOverlay(open, panel, {
        dismissible: true,
        onClose: () => {
          closes.push(Date.now());
          open.value = false;
        },
      });
      expose({ panel, open });
      return () =>
        open.value
          ? h("div", { ref: panel, tabindex: -1 }, [
              h("button", { type: "button" }, "btn-1"),
              h("button", { type: "button" }, "btn-2"),
            ])
          : h("div");
    },
  });
  const wrapper = mount(Host, { attachTo: document.body });
  return { wrapper, open, closes };
}

function pressEsc() {
  const ev = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  document.dispatchEvent(ev);
}

describe("useOverlay", () => {
  afterEach(() => {
    document.body.style.overflow = "";
  });

  it("Esc 关闭单层 overlay", async () => {
    const { wrapper, open, closes } = makeHost(true);
    await nextTick();
    expect(open.value).toBe(true);
    pressEsc();
    await nextTick();
    expect(closes.length).toBe(1);
    expect(open.value).toBe(false);
    wrapper.unmount();
  });

  it("嵌套 overlay 时 Esc 只关栈顶 (内层), 外层不动", async () => {
    const outer = makeHost(true);
    await nextTick();
    const inner = makeHost(true);
    await nextTick();

    pressEsc();
    await nextTick();

    expect(inner.closes.length).toBe(1);
    expect(inner.open.value).toBe(false);
    expect(outer.closes.length).toBe(0);
    expect(outer.open.value).toBe(true);

    pressEsc();
    await nextTick();
    expect(outer.closes.length).toBe(1);
    expect(outer.open.value).toBe(false);

    inner.wrapper.unmount();
    outer.wrapper.unmount();
  });

  it("多 overlay 叠开时 body overflow 计数解锁", async () => {
    expect(document.body.style.overflow).toBe("");
    const a = makeHost(true);
    await nextTick();
    expect(document.body.style.overflow).toBe("hidden");
    const b = makeHost(true);
    await nextTick();
    expect(document.body.style.overflow).toBe("hidden");
    b.open.value = false;
    await nextTick();
    // 还有 a 没关, 仍然锁住
    expect(document.body.style.overflow).toBe("hidden");
    a.open.value = false;
    await nextTick();
    expect(document.body.style.overflow).toBe("");
    a.wrapper.unmount();
    b.wrapper.unmount();
  });

  it("unmount 时若仍 open, 应当从栈中清掉, 后续 Esc 无副作用", async () => {
    const { wrapper, open } = makeHost(true);
    await nextTick();
    wrapper.unmount();
    // 不应抛错, 且 body 锁应被释放
    expect(() => pressEsc()).not.toThrow();
    expect(document.body.style.overflow).toBe("");
    expect(open.value).toBe(true); // 我们没动它, 但 controller 已脱离 stack
  });

  it("non-dismissible 的 overlay Esc 不关", async () => {
    const open = ref(true);
    let closeCount = 0;
    const Host = defineComponent({
      setup() {
        const panel = ref<HTMLElement | null>(null);
        useOverlay(open, panel, {
          dismissible: false,
          onClose: () => closeCount++,
        });
        return () =>
          open.value
            ? h("div", { ref: panel, tabindex: -1 })
            : h("div");
      },
    });
    const wrapper = mount(Host, { attachTo: document.body });
    await nextTick();
    pressEsc();
    await nextTick();
    expect(closeCount).toBe(0);
    expect(open.value).toBe(true);
    wrapper.unmount();
  });
});
