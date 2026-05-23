<template>
  <canvas
    v-if="shouldRender"
    ref="canvas"
    class="decor-firefly"
    aria-hidden="true"
  />
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { useRafSchedule } from "@/composables/useRafSchedule";

type Density = "full" | "subtle" | "off";

const props = withDefaults(
  defineProps<{ density?: Density }>(),
  { density: "subtle" },
);

const canvas = ref<HTMLCanvasElement | null>(null);
let rafHandle = 0;
let dots: Dot[] = [];
let lastTime = 0;
let lastDraw = 0;
let viewW = 0;
let viewH = 0;
let dpr = 1;

/**
 * 帧率封顶到 30fps. 萤火/花瓣本身漂得很慢, 60fps 下肉眼看不出和
 * 30fps 的差别, 但 GPU+CPU 工作量直接减半. 这是手机端发热的最
 * 直接根源之一 — 移动 GPU 在持续 60fps 全屏 canvas + mix-blend
 * 下会维持高频时钟。
 */
const FRAME_MS = 1000 / 30;

const count = computed(() =>
  props.density === "full" ? 22 : props.density === "subtle" ? 11 : 0,
);
const shouldRender = computed(() => props.density !== "off" && !prefersReducedMotion());

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

type Dot = {
  x: number;
  y: number;
  vy: number;
  phase: number;
  drift: number;
  r: number;
  toneIdx: number;
  twinkle: number;
};

const TONES = [
  "rgba(255,234,180,%A)",
  "rgba(255,216,140,%A)",
  "rgba(240,200,255,%A)",
];

/**
 * ─────────── 精灵 (sprite) 缓存 ───────────
 *
 * 重构前每个粒子每一帧都 createRadialGradient + arc + fill, 11–22 次/帧
 * 一直跑. createRadialGradient 在 canvas 2D 里是最贵的几个调用之一 —
 * 等价"每帧重建 22 张小贴图".
 *
 * 现在改成: 启动时把每种 tone 渲染一张 96×96 的 off-screen canvas,
 * 每帧只做 drawImage + globalAlpha. drawImage 在所有 GPU 加速实现里
 * 都走纹理路径, 单调用 ~10–50× 于 fillRect+gradient.
 *
 * sprite 内部:
 *   • 半径=半幅, 与原 draw() 的 halo 半径 = p.r*6 一致, 后续 drawImage
 *     用 d = p.r*12 缩放
 *   • 半透明渐变 stops 与原一致 (1 → 0.3 → 0), 整体 alpha=1, 由
 *     globalAlpha 在 draw 时按 twinkle 调制
 *   • 中心实心点 半径=半幅/6, 对应原 core dot p.r*1
 */
const SPRITE_PX = 96;
let sprites: HTMLCanvasElement[] = [];

function buildSprites() {
  if (sprites.length === TONES.length) return;
  sprites = TONES.map((tone) => {
    const c = document.createElement("canvas");
    c.width = c.height = SPRITE_PX;
    const cx = c.getContext("2d")!;
    const half = SPRITE_PX / 2;
    const grd = cx.createRadialGradient(half, half, 0, half, half, half);
    grd.addColorStop(0,   tone.replace("%A", "1"));
    grd.addColorStop(0.4, tone.replace("%A", "0.3"));
    grd.addColorStop(1,   tone.replace("%A", "0"));
    cx.fillStyle = grd;
    cx.beginPath();
    cx.arc(half, half, half, 0, Math.PI * 2);
    cx.fill();
    cx.fillStyle = tone.replace("%A", "1");
    cx.beginPath();
    cx.arc(half, half, half / 6, 0, Math.PI * 2);
    cx.fill();
    return c;
  });
}

function spawn(w: number, h: number, initial = false): Dot {
  return {
    x: Math.random() * w,
    y: initial ? Math.random() * h : h + 10 + Math.random() * 30,
    vy: -10 - Math.random() * 18,
    phase: Math.random() * Math.PI * 2,
    drift: 8 + Math.random() * 22,
    r: 1.2 + Math.random() * 2.4,
    toneIdx: Math.floor(Math.random() * TONES.length),
    twinkle: Math.random() * Math.PI * 2,
  };
}

/** 仅调整 canvas backing store, 不重撒。 */
function resizeCanvas(el: HTMLCanvasElement) {
  // DPR 收紧到 1.5 — 移动设备绝大多数是 dpr 2 或 3, 全屏 canvas 在 dpr 3
  // 上等价 9 倍像素填充. 1.5 在 retina 上肉眼不可分辨, 但填充率减 4×.
  dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (w === 0 || h === 0) return;
  el.width = Math.floor(w * dpr);
  el.height = Math.floor(h * dpr);
  const ctx = el.getContext("2d");
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (viewW > 0 && (w !== viewW || h !== viewH)) {
    for (const p of dots) {
      if (p.x > w + 20) p.x = Math.random() * w;
      if (p.y > h + 20) p.y = Math.random() * h;
    }
  }
  viewW = w;
  viewH = h;
}

function ensureDots(w: number, h: number) {
  const n = count.value;
  if (dots.length === n) return;
  if (dots.length < n) {
    while (dots.length < n) dots.push(spawn(w, h, true));
  } else {
    dots.length = n;
  }
}

function startLoop(el: HTMLCanvasElement) {
  const ctx = el.getContext("2d");
  if (!ctx) return;
  cancelAnimationFrame(rafHandle);
  lastTime = 0;
  lastDraw = 0;
  const tick = (t: number) => {
    if (!canvas.value) return;
    // 帧率封顶: 30fps 节流, 视觉上相同, 但 GPU 占空比腰斩.
    // 容差 -2ms 是为了把 60Hz/120Hz 的 rAF tick 都能稳定卡上 30fps.
    if (lastDraw && t - lastDraw < FRAME_MS - 2) {
      rafHandle = requestAnimationFrame(tick);
      return;
    }
    if (lastTime === 0) lastTime = t;
    const dt = Math.min((t - lastTime) / 1000, 0.05);
    lastTime = t;
    lastDraw = t;
    const w = viewW;
    const h = viewH;
    ctx.clearRect(0, 0, w, h);
    for (const p of dots) {
      p.y += p.vy * dt;
      p.phase += dt * 0.9;
      p.x += Math.sin(p.phase) * p.drift * dt;
      p.twinkle += dt * 2.2;
      if (p.y + p.r < -10) Object.assign(p, spawn(w, h, false));
      // a 与原 draw() 的 alpha 保持一致 (0.20 ~ 0.90).
      // 整张 sprite 在 globalAlpha 下被空间渐变成 a, 0.3a, 0 — 与
      // 原 createRadialGradient stop 配置完全等价.
      const a = 0.55 + Math.sin(p.twinkle) * 0.35;
      ctx.globalAlpha = a;
      const d = p.r * 12;
      ctx.drawImage(sprites[p.toneIdx], p.x - d / 2, p.y - d / 2, d, d);
    }
    ctx.globalAlpha = 1;
    rafHandle = requestAnimationFrame(tick);
  };
  rafHandle = requestAnimationFrame(tick);
}

function bootstrap(el: HTMLCanvasElement) {
  buildSprites();
  resizeCanvas(el);
  ensureDots(viewW, viewH);
  startLoop(el);
}

const resizeSched = useRafSchedule(() => {
  if (canvas.value) resizeCanvas(canvas.value);
});
function onResize() {
  resizeSched.trigger();
}

function onVisibility() {
  if (document.hidden) {
    cancelAnimationFrame(rafHandle);
    rafHandle = 0;
  } else if (canvas.value && !rafHandle) {
    startLoop(canvas.value);
  }
}

onMounted(() => {
  if (canvas.value) bootstrap(canvas.value);
  window.addEventListener("resize", onResize, { passive: true });
  document.addEventListener("visibilitychange", onVisibility);
});
onBeforeUnmount(() => {
  cancelAnimationFrame(rafHandle);
  // resizeSched 自己会在 unmount cancel
  window.removeEventListener("resize", onResize);
  document.removeEventListener("visibilitychange", onVisibility);
});

watch(
  () => props.density,
  () => {
    if (!canvas.value) return;
    ensureDots(viewW, viewH);
  },
);
</script>

<style scoped>
.decor-firefly {
  position: fixed;
  inset: 0;
  z-index: 1;
  pointer-events: none;
  width: 100%;
  height: 100%;
  mix-blend-mode: screen;
}
</style>
