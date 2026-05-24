<template>
  <!--
    "苦情树下" 舞台 · 图片承载版。
    层级：
      __sky    → 主背景画 + 主题滤镜
      __veil   → 极淡气流飘动雾色（呼吸）
      __overlay → 顶部光晕 + 底部色幕（让画面沉入 UI 底色）
      __bells  → 五个对应"画中铃"的光晕点（事件触发时金光涟漪）
      __mist   → 底部 38vh 渐变到 bg, 让对话区脱出
    aria-hidden 纯装饰。

    设计契约 — 底图静止:
      路由切换、主题切换都不再带过渡动画. 移动端 layout viewport 在
      地址栏开合时本身就有抖动, 主图再来个 background-position 过渡
      会和地址栏抖动叠加, 画面感觉"跳来跳去"。把底图当一张静态壁纸,
      只让上方雾层 (__veil) 做极慢的呼吸 — 那一层是 transform/opacity,
      走 GPU 合成层, 不影响 layout。
  -->
  <div class="deco-sakura" aria-hidden="true">
    <div class="deco-sakura__sky" />
    <div class="deco-sakura__veil" />
    <div class="deco-sakura__overlay" />

    <!-- 画中"铃位"涟漪。事件 ringBell(slot) 时让对应 slot 的金光涟漪展开一次。 -->
    <div class="deco-sakura__bells" aria-hidden="true">
      <span
        v-for="(b, i) in BELL_ANCHORS"
        :key="i"
        class="deco-sakura__bell"
        :class="{ 'is-celebrating': celebrateSlot === i }"
        :style="{ left: b.x + '%', top: b.y + '%' }"
      />
    </div>

    <div class="deco-sakura__mist" />
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";

import { useStageBus } from "@/composables/useStageBus";

/**
 * 画中铃的近似位置（相对画布百分比）。
 * 对应 useStageBus().ringBell(slot) 的 5 个槽。
 * 你换图后只要把这五个坐标手动调一次, 不影响其他逻辑。
 */
const BELL_ANCHORS = [
  { x: 22, y: 34 }, // 左上 · 主枝下垂
  { x: 78, y: 28 }, // 右上 · 远枝
  { x: 14, y: 58 }, // 左中 · 树干内侧
  { x: 70, y: 56 }, // 右中
  { x: 46, y: 42 }, // 中央 · 主穗
];

// === 事件铃 ===
const { state } = useStageBus();
const celebrateSlot = ref(-1);
let clearHandle = 0;

watch(
  () => state.bellToken,
  () => {
    if (state.bellToken === 0) return;
    celebrateSlot.value = state.bellSlot;
    if (clearHandle) window.clearTimeout(clearHandle);
    clearHandle = window.setTimeout(() => {
      celebrateSlot.value = -1;
    }, 1100);
  },
);
</script>

<style scoped>
.deco-sakura {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 100vh;
  height: 100lvh;
  min-height: 100svh;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
  contain: layout paint size;
  transform: translateZ(0);
}

/* —— 主画 —— */
.deco-sakura__sky {
  position: absolute;
  inset: 0;
  background-color: rgb(var(--color-bg));
  background-image: var(--decor-image);
  background-size: cover;
  background-repeat: no-repeat;
  background-position: 30% 42%;
  filter: var(--decor-img-filter, none);
  /*
   * 主图静态. 早先这里有 background-position 与 filter 的过渡, 用于
   * "路由切换时背景画面缓滑位移"+ "主题切换时滤镜软切换" 两种动效.
   * 移动端实测两个动效都是抖动来源 — 路由变化在 SPA 里很频繁, 每次
   * 都让全屏 background 重排; 主题切换也会带 700ms 的滤镜插值.
   * 现在路由 / 主题改变就是瞬时换底, 画面纹丝不动. 视觉损失:
   * 没有"图缓滑"的高级感, 但移动端的稳定感更值钱.
   */
}

/* —— 极淡气流呼吸（雾扫过画面）—— */
.deco-sakura__veil {
  position: absolute;
  inset: -6%;
  background-image: var(--mist-gradient);
  background-size: 100% 100%;
  background-repeat: no-repeat;
  mix-blend-mode: var(--decor-blend, screen);
  /*
   * opacity 由 keyframes 在 --veil-min ~ --veil-max 之间循环.
   * 这两个变量在主题块里覆盖, dark 用 0.42/0.58, light 用 0.32/0.46.
   */
  --veil-min: 0.42;
  --veil-max: 0.58;
  animation: var(--motion-breeze-drift);
  pointer-events: none;
  /*
   * 提到独立合成层 — transform/opacity 动画在合成器线程跑, 跳过 paint.
   * will-change 在这里是常驻的可接受代价, 因为这一层本来就一直在动,
   * 提层后几乎所有动画成本从主线程挪到 GPU.
   */
  will-change: transform, opacity;
  transform: translateZ(0);
}
:root[data-theme="light"] .deco-sakura__veil,
:root[data-theme="auto"] .deco-sakura__veil {
  /* 浅色主题用 multiply 把雾沉入纸面 */
  mix-blend-mode: multiply;
  --veil-min: 0.32;
  --veil-max: 0.46;
}

/* —— 色幕：顶部一束光 + 底部沉入 UI 底色 —— */
.deco-sakura__overlay {
  position: absolute;
  inset: 0;
  background-image: var(--sky-overlay);
  background-size: 100% 100%, 100% 100%, 100% 100%;
  background-repeat: no-repeat;
  pointer-events: none;
}

/* —— 画中"铃位"金光涟漪 —— */
.deco-sakura__bells {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.deco-sakura__bell {
  position: absolute;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  transform: translate(-50%, -50%) scale(0);
  background: radial-gradient(
    circle at 50% 50%,
    rgb(var(--color-bell) / .9) 0%,
    rgb(var(--color-bell) / .35) 35%,
    rgb(var(--color-bell) / 0) 75%
  );
  opacity: 0;
  filter: blur(0.4px);
}
.deco-sakura__bell.is-celebrating {
  animation: bell-ripple 1.05s cubic-bezier(0.18, 0.6, 0.2, 1) 1;
}

@keyframes bell-ripple {
  0%   { transform: translate(-50%, -50%) scale(0.2); opacity: 0; }
  18%  { transform: translate(-50%, -50%) scale(1);   opacity: 1; }
  70%  { transform: translate(-50%, -50%) scale(8);   opacity: 0.55; }
  100% { transform: translate(-50%, -50%) scale(14);  opacity: 0; }
}

/* —— 底部雾向 UI 底色过渡 —— */
.deco-sakura__mist {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 38vh;
  pointer-events: none;
  background: linear-gradient(
    to top,
    rgb(var(--color-bg)) 0%,
    rgb(var(--color-bg) / .82) 22%,
    rgb(var(--color-bg) / .42) 56%,
    rgb(var(--color-bg) / 0) 100%
  );
}

@media (max-width: 720px) {
  .deco-sakura__sky {
    /* 移动端：把焦点上提, 不被对话区盖住 */
    background-size: cover;
    background-position: 28% 32%;
  }
  /*
   * 移动端把雾层呼吸停掉. transform/opacity 走 GPU 合成层的代价虽小,
   * 但 30s 周期持续帧合成在中端 Android 上仍是常驻 GPU 时钟拉高的因素.
   * 静态雾在 38vh mist 与 sky-overlay 的色幕加持下视觉差极小.
   */
  .deco-sakura__veil {
    animation: none;
    opacity: 0.5;
  }
}

@media (prefers-reduced-motion: reduce) {
  .deco-sakura__veil,
  .deco-sakura__bell {
    animation: none !important;
  }
  .deco-sakura__sky {
    animation: none !important;
  }
}
</style>
