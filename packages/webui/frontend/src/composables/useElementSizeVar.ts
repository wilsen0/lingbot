import { onBeforeUnmount, watch, type Ref } from "vue";

/**
 * 把 DOM 元素的尺寸 (默认 offsetHeight) 通过 ResizeObserver 写到 :root
 * 的某个 CSS 变量。
 *
 * ─────────── 设计动机 ───────────
 *
 * 之前 Chat.vue 把 dockEl.offsetHeight 读到 ref<number>, 再绑到 main 元素
 * 的 :style="{ paddingBottom: ... }". 这条链有两个根本毛病:
 *
 *   1. 长度信息天然属于 CSS 变量 — 把它经过 reactive ref 是为了让 Vue
 *      Patch DOM, 但接收方就是 CSS, 多此一举。
 *   2. 每次输入条变高都触发 Vue 一次 re-render + style patch, 在快速键入
 *      时产生看得见的 jank。
 *
 * 正确做法 (本 composable): ResizeObserver 直接 setProperty 到 :root,
 * CSS 走自己的合成层, 完全跳过 Vue 反应式系统。
 *
 * ─────────── 契约 ───────────
 * 一个 cssVar 同一时刻只允许一处写入。如果多个 useElementSizeVar 写同一
 * 变量, 行为未定义 (谁后 attach 谁覆盖, 任意一处 detach 都会清掉)。
 * 业务侧自己保证唯一性 (例如 "--chat-dock-h" 只能由 ChatComposer 写)。
 *
 * 用法:
 *
 *   const dockEl = ref<HTMLElement | null>(null);
 *   useElementSizeVar(dockEl, "--chat-dock-h", { kind: "height", min: 72 });
 *
 *   // CSS:
 *   //   .chat__main { padding-bottom: calc(var(--chat-dock-h, 96px) + 24px); }
 */

type Kind = "height" | "width";

interface Options {
  /** 测哪一边. 默认 height. */
  kind?: Kind;
  /** 写入前的下限, 防止初始 0 / 折叠态写出错乱值. */
  min?: number;
  /** 写入前的上限. */
  max?: number;
}

export function useElementSizeVar(
  elRef: Ref<HTMLElement | null>,
  cssVar: string,
  opts: Options = {},
): void {
  const kind = opts.kind ?? "height";
  let observer: ResizeObserver | null = null;
  /** 退化路径: 没 ResizeObserver 时用 window.resize, 这里保存 listener 以便卸载 */
  let fallbackHandler: (() => void) | null = null;
  let hasWritten = false;

  function measure(el: HTMLElement) {
    let v = kind === "height" ? el.offsetHeight : el.offsetWidth;
    if (typeof opts.min === "number") v = Math.max(opts.min, v);
    if (typeof opts.max === "number") v = Math.min(opts.max, v);
    document.documentElement.style.setProperty(cssVar, v + "px");
    hasWritten = true;
  }

  function attach(el: HTMLElement) {
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => measure(el));
      observer.observe(el);
      measure(el);
      return;
    }
    // 退化: 仅 window resize. 保存 handler 引用以便 detach 时正确移除。
    fallbackHandler = () => measure(el);
    window.addEventListener("resize", fallbackHandler, { passive: true });
    measure(el);
  }

  function detach() {
    observer?.disconnect();
    observer = null;
    if (fallbackHandler) {
      window.removeEventListener("resize", fallbackHandler);
      fallbackHandler = null;
    }
  }

  watch(
    elRef,
    (el) => {
      detach();
      if (el) attach(el);
    },
    { immediate: true, flush: "post" },
  );

  onBeforeUnmount(() => {
    detach();
    if (hasWritten) {
      // 只有写过才清; 否则可能误删另一处同名 var
      document.documentElement.style.removeProperty(cssVar);
    }
  });
}
