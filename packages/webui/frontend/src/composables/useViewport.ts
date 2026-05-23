import { createRafSchedule } from "@/composables/useRafSchedule";

/**
 * 视窗几何追踪 / 输入法回正
 *
 * ─────────── 第一性原理 ───────────
 * 移动端键盘弹起时, 我们要做的事只有一件: 让"该浮在键盘之上的 UI"
 * 知道键盘遮了多少像素。其余一切派生:
 *
 *   • 输入条 dock / sheet / drawer 用
 *     transform: translateY(calc(-1 * var(--vv-bottom)))
 *     抬到键盘上方
 *   • 滚动容器 (Chat 的 main) 用 scroll-padding-bottom: var(--vv-bottom)
 *     让浏览器自己的"焦点滚到可见区"启发式知道避开键盘 — 这比我们手写
 *     scrollIntoView 更鲁棒, 因为它和原生行为同源
 *   • 配合 viewport meta `interactive-widget=overlays-content`, layout
 *     viewport 不变, 所以背景图、消息列表、装饰层位置都不动
 *
 * ─────────── 之前的坑 ───────────
 *
 * 早期实现做了三件多余的事, 这些事在桌面看不出来, 在手机上每一件都会
 * 让画面"跳来跳去":
 *
 *   1) **写 --vv-top / --vv-height**. 没人消费, 单纯的死代码 — 但每次
 *      visualViewport 抖动都会让 :root 上写三条 var 而不是一条, 绑了
 *      它的元素都会被样式重算波及。删除。
 *
 *   2) **键盘开期间挂 visualViewport.scroll 监听**. 看起来"为了追踪
 *      地址栏抖动", 实则把 iOS Safari 滚动期间的所有 vv.scroll 事件
 *      都翻译成 var 改写 → 触发 composer transform / msg-list
 *      scroll-padding 等元素的样式重算; 这就是用户看到的"跳来跳去"。
 *      地址栏伸缩本身就是不稳定的源头, 我们去监听只会放大它。
 *      只在 resize (键盘真的开/关时才会发的事件) 上更新 var, 滚动期间
 *      让 layout 保持静止。
 *
 *   3) **像素级精度**. visualViewport 在键盘开合动画中每帧都给小数变化,
 *      Math.round 之后还是会出现 0/1/0 这样的高频抖动 — 元素 transform
 *      跟着抖。把 vv-bottom 量化到 4 像素步进就没了, 视觉差异肉眼不可察。
 *
 * ─────────── CSS 变量契约 ───────────
 *   --vv-bottom : 视觉可见区底部相对 layout viewport 底部的距离 (键盘高度,
 *                 量化到 4px 步进)
 *   data-keyboard="open" : 当键盘弹起时挂在 <html> 上, 给 CSS 选择器使用
 *
 * ─────────── 生命周期模型 ───────────
 * useViewport() 是"应用级"单例 — 整个文档生命周期内只装一次, 不挂载到
 * 任何组件的 onBeforeUnmount。监听器随页面关闭由浏览器一并回收。
 */

interface VVInfo {
  /** 视觉可见区底部相对 layout viewport 底部的距离, 单位 px (>=0). */
  bottom: number;
}

/** 阈值: 避开 iOS 软导航条 / 地址栏抖动等小幅遮挡 */
const KEYBOARD_THRESHOLD_PX = 80;
/** 量化步长: vv-bottom 写入前先按这个粒度对齐. 减少键盘动画期间的微抖. */
const QUANTIZE_PX = 4;

function quantize(v: number): number {
  return Math.round(v / QUANTIZE_PX) * QUANTIZE_PX;
}

function read(): VVInfo {
  if (typeof window === "undefined") return { bottom: 0 };
  const vv = window.visualViewport;
  if (!vv) return { bottom: 0 };
  /*
   * 键盘高度 = layout viewport 底 - (visualViewport 顶 + visualViewport 高).
   *
   * iOS Safari 默认行为 (interactive-widget=resizes-visual) 下:
   *   window.innerHeight 是 layout viewport 高度 (键盘开/关都不变)
   *   vv.height 是当前可视区高度 (键盘上方那块)
   *   vv.offsetTop 是可视区顶部相对 layout viewport 的偏移 — 通常是 0,
   *     但用户在键盘上方滚动 / Safari 自动把焦点滚到中心时会非零
   *
   * 早先版本不减 vv.offsetTop 的偏置 — 当 Safari 自己把焦点滚到中央时
   * vv.offsetTop > 0, 我们仍然算 (innerHeight - 0 - vv.height) 就把
   * "可视区顶部到 layout 顶部的距离"当成了"键盘高度", 多算了一倍, dock
   * 被抬过头, 出现 ~150px 的空白. 减去 offsetTop 才是真正的"键盘+底部
   * 不可视区"高度.
   *
   * 注意: 这里我们仍然不监听 vv.scroll. offsetTop 在 resize 触发那一帧
   * 取一次就行, 滚动期间继续跟会回到老的抖动问题.
   */
  const layoutH = window.innerHeight;
  const visualBottom = vv.offsetTop + vv.height; // 可视区底 (相对 layout 顶)
  const bottom = Math.max(0, Math.round(layoutH - visualBottom));
  return { bottom };
}

let lastWritten = -1;

function applyToCss(info: VVInfo) {
  const r = document.documentElement;
  const q = quantize(info.bottom);
  if (q !== lastWritten) {
    r.style.setProperty("--vv-bottom", q + "px");
    lastWritten = q;
  }
  if (info.bottom > KEYBOARD_THRESHOLD_PX) {
    if (r.dataset.keyboard !== "open") r.dataset.keyboard = "open";
  } else if (r.dataset.keyboard) {
    delete r.dataset.keyboard;
  }
}

let installed = false;
let teardown: (() => void) | null = null;

/**
 * 应用级单例; 多次调用幂等, 不在组件卸载时拆。
 * 推荐在根组件 (App.vue) 的 setup 里调用一次, 但任意位置调用都安全。
 */
export function useViewport(): void {
  if (installed) return;
  if (typeof window === "undefined") return;
  installed = true;

  const apply = () => applyToCss(read());
  const sched = createRafSchedule(apply);
  apply();

  const vv = window.visualViewport;
  if (vv) {
    // 仅监听 resize (键盘开/关、屏幕旋转) — 不再监听 scroll。
    // 滚动期间地址栏伸缩本身就是抖动源, 监听会把抖动翻译成 var 改写,
    // 导致绑了 var 的元素全部抖。
    vv.addEventListener("resize", sched.trigger);
  } else {
    window.addEventListener("resize", sched.trigger);
  }
  window.addEventListener("orientationchange", sched.trigger);

  teardown = () => {
    sched.cancel();
    if (vv) {
      vv.removeEventListener("resize", sched.trigger);
    } else {
      window.removeEventListener("resize", sched.trigger);
    }
    window.removeEventListener("orientationchange", sched.trigger);
    installed = false;
  };
}

/**
 * 仅供测试 / HMR 使用 — 强制拆掉单例, 让下一次 useViewport() 重装。
 * 业务代码不应调用。
 */
export function _resetViewportForTest(): void {
  teardown?.();
  teardown = null;
  lastWritten = -1;
}
