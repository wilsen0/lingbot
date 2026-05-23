<template>
  <canvas
    v-if="shouldRender"
    ref="canvas"
    class="decor-petal"
    aria-hidden="true"
  />
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { useRafSchedule } from "@/composables/useRafSchedule";

type Density = "full" | "subtle" | "off";

const props = withDefaults(
  defineProps<{
    density?: Density;
  }>(),
  { density: "subtle" },
);

const canvas = ref<HTMLCanvasElement | null>(null);
let rafHandle = 0;
let petals: Petal[] = [];
let lastTime = 0;
let lastDraw = 0;
let viewW = 0;
let viewH = 0;
let dpr = 1;

/**
 * 帧率封顶到 30fps. 花瓣下落速度 22-54 px/s, 30fps 步进约 0.7-1.8 px/帧,
 * 远小于一片花瓣的尺寸, 视觉上和 60fps 不可分辨, 但 GPU 占用减半.
 */
const FRAME_MS = 1000 / 30;

const maxParticles = computed(() =>
  props.density === "full" ? 42 : props.density === "subtle" ? 22 : 0,
);
const shouldRender = computed(() => props.density !== "off" && !prefersReducedMotion());

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

type Petal = {
  x: number;
  y: number;
  vy: number;       // 下落速度
  vx: number;       // 风速（慢变化）
  drift: number;    // 左右摆幅
  phase: number;    // 摆动相位
  size: number;
  rot: number;
  rotV: number;
  toneIdx: number;
  alpha: number;
  shape: 0 | 1 | 2;
};

const TONES = [
  "rgba(255,208,228,1)",
  "rgba(252,184,212,1)",
  "rgba(252,208,180,1)",
  "rgba(238,150,200,1)",
  "rgba(248,234,248,1)",
];

/**
 * ─────────── 精灵 (sprite) 缓存 ───────────
 *
 * 每片花瓣每帧调用三次 bezierCurveTo + ellipse + stroke + fill, 22-42 片
 * 一直跑, 是 GPU/CPU 烧火炉的另一只手.
 *
 * 改成: 启动时对 (shape × tone) 组合各画一张 off-screen sprite, 主循环
 * 只做 translate+rotate+drawImage. drawImage 是纹理快路径, 比 path 重画
 * 一个数量级快.
 *
 * sprite 内部按 NORM_SIZE 画, 实时绘制时 d = p.size / NORM_SIZE * SPRITE_PX
 * 缩放, 与原 ctx.translate(p.x,p.y) + path(s=p.size) 视觉等价.
 */
const NORM_SIZE = 12;        // sprite 内部使用的"标准 size"
const SPRITE_PX = 48;        // sprite 画布物理尺寸 (含描边外扩冗余)
type Sprite = HTMLCanvasElement;
let sprites: Sprite[][] = []; // [shape][toneIdx]

function buildSprite(shape: 0 | 1 | 2, tone: string): Sprite {
  const c = document.createElement("canvas");
  c.width = c.height = SPRITE_PX;
  const cx = c.getContext("2d")!;
  cx.translate(SPRITE_PX / 2, SPRITE_PX / 2);
  cx.fillStyle = tone;
  const s = NORM_SIZE;
  cx.beginPath();
  if (shape === 0) {
    cx.moveTo(0, -s);
    cx.bezierCurveTo(s * 0.92, -s * 0.78, s * 0.96, s * 0.42, 0, s);
    cx.bezierCurveTo(-s * 0.96, s * 0.42, -s * 0.92, -s * 0.78, 0, -s);
    cx.moveTo(0, -s);
    cx.quadraticCurveTo(s * 0.18, -s * 0.78, 0, -s * 0.78);
    cx.quadraticCurveTo(-s * 0.18, -s * 0.78, 0, -s);
  } else if (shape === 1) {
    cx.ellipse(0, 0, s * 0.9, s * 0.44, 0, 0, Math.PI * 2);
  } else {
    cx.moveTo(-s, 0);
    cx.quadraticCurveTo(0, -s * 0.84, s, 0);
    cx.quadraticCurveTo(0, s * 0.48, -s, 0);
  }
  cx.fill();
  cx.globalAlpha = 0.5;
  cx.strokeStyle = "rgba(255,255,255,.25)";
  cx.lineWidth = 0.6;
  cx.stroke();
  return c;
}

function buildSprites() {
  if (sprites.length) return;
  sprites = [0, 1, 2].map((shape) =>
    TONES.map((tone) => buildSprite(shape as 0 | 1 | 2, tone)),
  );
}

function spawn(w: number, h: number, initial = false): Petal {
  return {
    x: Math.random() * w,
    y: initial ? Math.random() * h : -20 - Math.random() * h * 0.25,
    vy: 22 + Math.random() * 32,
    vx: (Math.random() - 0.5) * 12,
    drift: 28 + Math.random() * 60,
    phase: Math.random() * Math.PI * 2,
    size: 7 + Math.random() * 9,
    rot: Math.random() * Math.PI * 2,
    rotV: (Math.random() - 0.5) * 1.6,
    toneIdx: Math.floor(Math.random() * TONES.length),
    alpha: 0.55 + Math.random() * 0.4,
    shape: Math.floor(Math.random() * 3) as 0 | 1 | 2,
  };
}

function drawPetal(ctx: CanvasRenderingContext2D, p: Petal) {
  const sprite = sprites[p.shape][p.toneIdx];
  const d = (p.size / NORM_SIZE) * SPRITE_PX;
  ctx.globalAlpha = p.alpha;
  ctx.translate(p.x, p.y);
  ctx.rotate(p.rot);
  ctx.drawImage(sprite, -d / 2, -d / 2, d, d);
  // 反向变换 — 比 save/restore 在每帧 22-42 次循环里更快, 不用入栈出栈
  ctx.rotate(-p.rot);
  ctx.translate(-p.x, -p.y);
}

/**
 * 仅调整 canvas 的 backing store 尺寸 + 缓存视口宽高。
 * 不会重撒粒子, 不会打断 RAF 循环。
 */
function resizeCanvas(el: HTMLCanvasElement) {
  // DPR 收紧到 1.5 (见 DecoFireflies 注释). 花瓣边缘有半透明描边, 1.5 与
  // 2/3 的视觉差异在移动屏上肉眼几乎不可分辨, 但填充率成倍下降.
  dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (w === 0 || h === 0) return;
  el.width = Math.floor(w * dpr);
  el.height = Math.floor(h * dpr);
  const ctx = el.getContext("2d");
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  // 视口缩小时把已飘出新视口的粒子拽回来, 不重撒
  if (viewW > 0 && (w !== viewW || h !== viewH)) {
    for (const p of petals) {
      if (p.x > w + 40) p.x = Math.random() * w;
      if (p.y > h + 40) p.y = Math.random() * h;
    }
  }
  viewW = w;
  viewH = h;
}

function ensurePetals(w: number, h: number) {
  const n = maxParticles.value;
  if (petals.length === n) return;
  if (petals.length < n) {
    while (petals.length < n) petals.push(spawn(w, h, true));
  } else {
    petals.length = n;
  }
}

function startLoop(el: HTMLCanvasElement) {
  const ctx = el.getContext("2d");
  if (!ctx) return;
  cancelAnimationFrame(rafHandle);
  // 重启时把 lastTime 拉到当前帧, 避免 dt 大跳导致"瞬移"
  lastTime = 0;
  lastDraw = 0;
  const tick = (t: number) => {
    if (!canvas.value) return;
    // 30fps 节流 — 见顶部注释
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
    for (const p of petals) {
      p.y += p.vy * dt;
      p.phase += dt * 1.1;
      p.x += (Math.sin(p.phase) * p.drift + p.vx) * dt;
      p.rot += p.rotV * dt;
      if (p.y - p.size > h) Object.assign(p, spawn(w, h, false));
      if (p.x < -40) p.x = w + 20;
      if (p.x > w + 40) p.x = -20;
      drawPetal(ctx, p);
    }
    ctx.globalAlpha = 1;
    rafHandle = requestAnimationFrame(tick);
  };
  rafHandle = requestAnimationFrame(tick);
}

function bootstrap(el: HTMLCanvasElement) {
  buildSprites();
  resizeCanvas(el);
  ensurePetals(viewW, viewH);
  startLoop(el);
}

const resizeSched = useRafSchedule(() => {
  if (canvas.value) resizeCanvas(canvas.value);
});
function onResize() {
  // 合并到下一帧, 避免 iOS chrome 收/放地址栏时的 resize 风暴
  resizeSched.trigger();
}

function onVisibility() {
  if (document.hidden) {
    cancelAnimationFrame(rafHandle);
    rafHandle = 0;
  } else if (canvas.value && !rafHandle) {
    // 续接动画, 不重撒粒子
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
    // 密度变了 — 调整粒子数组长度即可, 不打断现有粒子
    ensurePetals(viewW, viewH);
  },
);
</script>

<style scoped>
.decor-petal {
  position: fixed;
  inset: 0;
  z-index: 1;
  pointer-events: none;
  width: 100%;
  height: 100%;
  /* 跟随主题：light → multiply 沉入纸面，dark → screen 浮出夜色 */
  mix-blend-mode: var(--decor-blend, normal);
}
</style>
