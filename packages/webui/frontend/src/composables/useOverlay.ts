import {
  computed,
  nextTick,
  onBeforeUnmount,
  type ComputedRef,
  type MaybeRefOrGetter,
  type Ref,
  toValue,
  watch,
} from "vue";

/**
 * 模态层生命周期 — UiSheet / UiConfirmSheet / MoreDrawer 共享的"开关那一刻
 * 该做什么"全部抽到这里:
 *
 *   1. 打开时记下当前 focus, 等下次关闭归位 (focus restore)
 *   2. 打开时把焦点带到 panel 内第一个可聚焦元素 (或 panel 自身)
 *   3. Tab / Shift+Tab 在 panel 内循环 (focus trap)
 *   4. Esc 关闭 (若 dismissible)
 *   5. body scroll lock — 多 overlay 叠开时计数管理, 都关了才解锁
 *   6. 多 overlay 叠开时 Esc / Tab 仅由"最顶层"消费 — 见 OVERLAY_STACK
 *
 * 之前每处 (UiSheet / UiConfirmSheet / MoreDrawer) 各自手抄一份, 对不齐:
 *   • UiSheet 用 document.body.style.overflow + 自己的 focusables 查询
 *   • MoreDrawer 也是, 但选择器不同
 *   • UiConfirmSheet 还多了一个 body 滚动锁 ref
 *   • 三家都没处理"嵌套 overlay 时 Esc 同时关多层"的问题
 * 集中在此, 不再有歧义。
 *
 * 仅依赖原生 DOM API, 不依赖任何 UI 库, 按 SSR-safe 的方式编写。
 */

interface UseOverlayOptions {
  /** Esc / 外部点击关闭时回调; 不传则视为不可关 (例如必须点确认) */
  onClose?: () => void;
  /**
   * 是否允许 Esc 关闭。支持响应式 (Ref / getter), 默认 true。
   * 设 false 时焦点劫持仍生效, 仅 Esc 不再关。
   */
  dismissible?: MaybeRefOrGetter<boolean>;
}

/**
 * 多 overlay 叠开栈 — 仅栈顶 overlay 接管 Esc / Tab。
 * 这样修玉 sheet 上叠"焚此玉" confirm 时, Esc 只关 confirm, 不连带把 sheet 关掉。
 */
type OverlayController = {
  panel: () => HTMLElement | null;
  dismissible: () => boolean;
  close: () => void;
};
const OVERLAY_STACK: OverlayController[] = [];
let globalKeydownBound = false;

const FOCUSABLE_SELECTOR =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable]:not([contenteditable="false"])';

function focusables(panel: HTMLElement | null): HTMLElement[] {
  if (!panel) return [];
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

function onGlobalKey(ev: KeyboardEvent) {
  const top = OVERLAY_STACK[OVERLAY_STACK.length - 1];
  if (!top) return;
  if (ev.key === "Escape") {
    if (top.dismissible()) {
      ev.preventDefault();
      ev.stopPropagation();
      top.close();
    }
    return;
  }
  if (ev.key !== "Tab") return;
  const list = focusables(top.panel());
  if (!list.length) {
    top.panel()?.focus?.();
    ev.preventDefault();
    return;
  }
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  const inside = top.panel()?.contains(active) ?? false;
  if (ev.shiftKey && (active === first || !inside)) {
    last.focus();
    ev.preventDefault();
  } else if (!ev.shiftKey && (active === last || !inside)) {
    first.focus();
    ev.preventDefault();
  }
}

function bindGlobalKeydown() {
  if (globalKeydownBound || typeof document === "undefined") return;
  // capture: 早于业务层, 避免 Esc 冒泡时被无关 handler 吃掉
  document.addEventListener("keydown", onGlobalKey, true);
  globalKeydownBound = true;
}

function unbindGlobalKeydownIfEmpty() {
  if (!globalKeydownBound) return;
  if (OVERLAY_STACK.length > 0) return;
  document.removeEventListener("keydown", onGlobalKey, true);
  globalKeydownBound = false;
}

/** 全局 body lock 计数 — 多个 overlay 叠开时只在最后一个关闭时解锁。 */
let bodyLockCount = 0;
let savedBodyOverflow = "";

function lockBody() {
  if (typeof document === "undefined") return;
  if (bodyLockCount === 0) {
    savedBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  bodyLockCount += 1;
}

function unlockBody() {
  if (typeof document === "undefined") return;
  bodyLockCount = Math.max(0, bodyLockCount - 1);
  if (bodyLockCount === 0) {
    document.body.style.overflow = savedBodyOverflow;
    savedBodyOverflow = "";
  }
}

/**
 * 打开一个模态层.
 *
 * @param open    一个 Ref<boolean> / ComputedRef<boolean>; watch 它来同步状态
 * @param panelRef 模态主面板的 template ref (用于 focus 劫持 / Tab 循环)
 * @param opts    onClose / dismissible
 *
 * 注意:
 *   - watch 用 immediate: true, 这样初始 open=true 也能正确进入打开态
 *     (SSR 路径下 onBeforeMount/onMounted 才允许碰 document, 但此 watch
 *     的副作用都用 typeof document 守卫, SSR 安全)
 *   - 实例 unmount 时若仍处于打开态, 主动 detach (防止 stack 残留 / body 锁未释放)
 */
export function useOverlay(
  open: Ref<boolean> | ComputedRef<boolean>,
  panelRef: Ref<HTMLElement | null>,
  opts: UseOverlayOptions = {},
): void {
  const dismissibleRef = computed(() => {
    if (opts.dismissible === undefined) return true;
    return toValue(opts.dismissible);
  });

  let prevActive: HTMLElement | null = null;
  let active = false;
  let focusGeneration = 0;

  const controller: OverlayController = {
    panel: () => panelRef.value,
    dismissible: () => dismissibleRef.value,
    close: () => opts.onClose?.(),
  };

  function attach() {
    if (active) return;
    active = true;
    if (typeof document === "undefined") return;
    prevActive = document.activeElement as HTMLElement | null;
    OVERLAY_STACK.push(controller);
    bindGlobalKeydown();
    lockBody();
  }

  function detach() {
    if (!active) return;
    active = false;
    if (typeof document === "undefined") return;
    const idx = OVERLAY_STACK.indexOf(controller);
    if (idx >= 0) OVERLAY_STACK.splice(idx, 1);
    unbindGlobalKeydownIfEmpty();
    unlockBody();
    prevActive?.focus?.();
    prevActive = null;
  }

  watch(
    () => toValue(open),
    async (v) => {
      if (v) {
        attach();
        // 在 await 前后比对 active, 防止 await 期间外部把 open 切回 false
        // 又重新打开时, 老的回调把焦点抢走 (race)。
        const generation = ++focusGeneration;
        await nextTick();
        if (generation !== focusGeneration || !active) return;
        const list = focusables(panelRef.value);
        (list[0] ?? panelRef.value)?.focus?.();
      } else {
        detach();
      }
    },
    { immediate: true, flush: "post" },
  );

  onBeforeUnmount(() => {
    // 组件 unmount 时若仍 active, 兜底清理 stack / body lock
    detach();
  });
}
