<template>
  <section class="set">
    <!-- 在册 · bots -->
    <section class="set__block">
      <header class="set__head">
        <h2 class="set__title font-display">已接入</h2>
        <p class="set__hint">已接入的 bot · 平台 · 在线状态</p>
      </header>

      <ul v-if="bots.length" class="set__bots">
        <li
          v-for="b in bots"
          :key="b.id"
          class="set__bot"
        >
          <span class="set__bot-dot" :class="b.online ? 'on' : 'off'" aria-hidden="true" />
          <div class="set__bot-info">
            <p class="set__bot-name font-display">{{ b.name || b.id }}</p>
            <p class="set__bot-meta font-mono">{{ b.platform }} · {{ b.id }}</p>
          </div>
        </li>
      </ul>
      <UiEmptyState v-else variant="compact">暂无 bot 接入</UiEmptyState>

      <p v-if="auth.profile?.role" class="set__role-line">
        <span class="set__role-mark" aria-hidden="true">·</span>
        <span
          >当前角色：<strong>{{ roleLabel }}</strong></span
        >
      </p>
    </section>

    <!-- 时辰 -->
    <section class="set__block">
      <header class="set__head">
        <h2 class="set__title font-display">外观</h2>
        <p class="set__hint">界面风格 · 随系统、浅色、深色三选一</p>
      </header>

      <div class="set__themes" role="radiogroup" aria-label="外观">
        <button
          v-for="t in themes"
          :key="t.value"
          role="radio"
          class="theme-card tap"
          :class="`theme-card--${t.value}`"
          :aria-checked="prefs.theme === t.value"
          :aria-label="t.aria"
          @click="prefs.setTheme(t.value)"
        >
          <span class="theme-card__sky" aria-hidden="true">
            <svg viewBox="0 0 36 36" fill="none">
              <circle v-if="t.value === 'auto'" cx="18" cy="18" r="14" fill="url(#sg-auto)" />
              <circle v-if="t.value === 'light'" cx="18" cy="18" r="14" fill="url(#sg-light)" />
              <circle v-if="t.value === 'dark'" cx="18" cy="18" r="14" fill="url(#sg-dark)" />
              <defs>
                <linearGradient id="sg-auto" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0%" stop-color="#3A2846" />
                  <stop offset="100%" stop-color="#FCE4B0" />
                </linearGradient>
                <radialGradient id="sg-light" cx="40%" cy="36%" r="65%">
                  <stop offset="0%" stop-color="#FCE4B0" />
                  <stop offset="60%" stop-color="#FFCFE2" />
                  <stop offset="100%" stop-color="#F6F0F8" />
                </radialGradient>
                <radialGradient id="sg-dark" cx="50%" cy="40%" r="60%">
                  <stop offset="0%" stop-color="#5C4288" />
                  <stop offset="55%" stop-color="#3C2C5C" />
                  <stop offset="100%" stop-color="#201832" />
                </radialGradient>
              </defs>
              <!-- 一弯枝在 sky 上 -->
              <path
                d="M6 26 C 14 22 22 24 30 18"
                stroke="rgb(var(--color-ink) / .55)"
                stroke-width="0.9"
                fill="none"
                stroke-linecap="round"
              />
            </svg>
          </span>
          <span class="theme-card__label font-display">{{ t.label }}</span>
          <span class="theme-card__hint">{{ t.hint }}</span>
        </button>
      </div>
    </section>

    <!-- 装饰 -->
    <section class="set__block">
      <header class="set__head">
        <h2 class="set__title font-display">动效</h2>
        <p class="set__hint">背景花瓣、雾色、萤光的强度</p>
      </header>

      <div class="set__decors" role="radiogroup" aria-label="动效强度">
        <button
          v-for="d in decors"
          :key="d.value"
          role="radio"
          class="decor-card tap"
          :aria-checked="prefs.decor === d.value"
          :aria-label="d.aria"
          @click="prefs.setDecor(d.value)"
        >
          <span class="decor-card__art" aria-hidden="true">
            <svg viewBox="0 0 40 28" fill="none">
              <!-- 满目：5 瓣；淡雅：2 瓣；静寂：0 瓣只一线 -->
              <g v-if="d.value !== 'off'" fill="rgb(var(--color-petal))">
                <ellipse cx="6" cy="6" rx="3" ry="1.6" transform="rotate(20 6 6)" />
                <ellipse cx="14" cy="14" rx="3" ry="1.6" transform="rotate(-15 14 14)" />
                <ellipse
                  v-if="d.value === 'full'"
                  cx="22"
                  cy="8"
                  rx="3"
                  ry="1.6"
                  transform="rotate(30 22 8)"
                />
                <ellipse
                  v-if="d.value === 'full'"
                  cx="30"
                  cy="20"
                  rx="3"
                  ry="1.6"
                  transform="rotate(-20 30 20)"
                />
                <ellipse
                  v-if="d.value === 'full'"
                  cx="36"
                  cy="12"
                  rx="3"
                  ry="1.6"
                  transform="rotate(15 36 12)"
                />
              </g>
              <path
                d="M2 24 C 12 20 24 26 38 22"
                stroke="rgb(var(--color-thread))"
                stroke-width="0.9"
                fill="none"
                stroke-linecap="round"
                opacity="0.8"
              />
            </svg>
          </span>
          <span class="decor-card__label font-display">{{ d.label }}</span>
          <span class="decor-card__hint">{{ d.hint }}</span>
        </button>
      </div>

      <p class="set__note">系统开启“减弱动画”时，装饰会自动关闭；背景仍会缓慢变化。</p>
    </section>

    <!-- 声与振 -->
    <section class="set__block">
      <header class="set__head">
        <h2 class="set__title font-display">提示音 · 震动</h2>
        <p class="set__hint">完成操作时提示一下；移动端轻震 15ms</p>
      </header>

      <div class="set__switch-row">
        <div class="set__switch-info">
          <p class="set__switch-name font-display">提示音</p>
          <p class="set__switch-sub">回复时发出提示音</p>
        </div>
        <UiSwitch
          :model-value="prefs.bellSound"
          on-label="开"
          off-label="关"
          label="提示音开关"
          @update:model-value="prefs.toggleBellSound()"
        />
      </div>
      <div class="set__switch-row">
        <div class="set__switch-info">
          <p class="set__switch-name font-display">震动</p>
          <p class="set__switch-sub">触发时轻震一下（手机）</p>
        </div>
        <UiSwitch
          :model-value="prefs.haptics"
          on-label="开"
          off-label="关"
          label="震动开关"
          @update:model-value="prefs.toggleHaptics()"
        />
      </div>
    </section>

    <!-- 版本 / 解缘 -->
    <section class="set__about">
      <p class="set__version font-mono">
        linling-webui · v{{ serverVersion ?? "——" }}
        <span v-if="serverTime" class="set__version-sub">· {{ serverTime }}</span>
      </p>
      <button class="set__logout tap" @click="onLogout">
        <span class="set__logout-mark" aria-hidden="true">·</span>
        <span class="set__logout-label font-display">退出</span>
        <span class="set__logout-hint">退出登录</span>
      </button>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { logout as apiLogout, getHealth } from "@/api/client";
import { listBots, type BotInfo } from "@/api/bots";
import UiEmptyState from "@/components/UiEmptyState.vue";
import UiSwitch from "@/components/UiSwitch.vue";
import { confirmDestructive } from "@/composables/useConfirm";
import { useAuthStore } from "@/store/auth";
import { usePrefsStore, type DecorLevel, type ThemeMode } from "@/store/prefs";

const prefs = usePrefsStore();
const auth = useAuthStore();
const router = useRouter();

const serverVersion = ref<string | null>(null);
const serverTime = ref<string | null>(null);

const roleLabel = computed(() => {
  switch (auth.profile?.role) {
    case "superadmin":
      return "管理员";
    case "bot_admin":
      return "机器人管理员";
    case "readonly":
      return "只读";
    default:
      return "";
  }
});

const themes: { value: ThemeMode; label: string; hint: string; aria: string }[] = [
  { value: "auto", label: "系统", hint: "跟随系统", aria: "跟随系统" },
  { value: "light", label: "浅色", hint: "浅色主题", aria: "浅色主题" },
  { value: "dark", label: "深色", hint: "深色主题", aria: "深色主题" },
];
const decors: { value: DecorLevel; label: string; hint: string; aria: string }[] = [
  { value: "full", label: "完整", hint: "效果全开", aria: "完整效果" },
  { value: "subtle", label: "简洁", hint: "保留轻量动效", aria: "轻量效果" },
  { value: "off", label: "关闭", hint: "不显示装饰", aria: "关闭动效" },
];

const bots = ref<BotInfo[]>([]);

function formatServerTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

onMounted(async () => {
  try {
    bots.value = await listBots();
  } catch {
    /* noop */
  }
  try {
    const h = await getHealth();
    serverVersion.value = h.version;
    serverTime.value = formatServerTime(h.time);
    if (Array.isArray(h.bots) && h.bots.length) bots.value = h.bots;
  } catch {
    /* noop */
  }
});

async function onLogout() {
  const ok = await confirmDestructive("退出登录", "退出后需要重新登录。", "退出");
  if (!ok) return;
  try {
    if (auth.refreshToken) await apiLogout(auth.refreshToken);
  } catch {
    /* logout API 失败也照样清本地 */
  } finally {
    auth.clear();
    await router.replace("/login");
  }
}
</script>

<style scoped>
.set {
  padding: var(--pad-y) var(--pad-x) calc(env(safe-area-inset-bottom, 0) + 36px);
  width: 100%;
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: clamp(28px, 6vw, 40px);
  /* 入场由 AppShell 的 page-layer router-transition 接管, 这里不再二次淡入 */
}
.set > * {
  width: min(100%, 780px);
}

/* ===== block ===== */
.set__block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.set__head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.set__title {
  font-size: clamp(20px, 5.5vw, 24px);
  letter-spacing: 0.18em;
  color: rgb(var(--color-ink));
  line-height: 1;
  text-shadow: 0 1px 2px rgb(0 0 0 / 0.25);
}
.set__hint {
  font-size: 12px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft));
}
.set__role-line {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: 12px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}
.set__role-line strong {
  color: rgb(var(--color-thread));
  font-weight: 500;
  margin-left: 2px;
}
.set__role-mark {
  color: rgb(var(--color-thread));
  font-size: 16px;
  line-height: 1;
}
.set__note {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
  margin-top: 6px;
  line-height: 1.7;
}

/* ===== bots ===== */
.set__bots {
  display: flex;
  flex-direction: column;
  margin-top: 6px;
  padding: 0 16px;
  list-style: none;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.78), rgb(var(--color-bg-veil) / 0.54));
  backdrop-filter: blur(14px);
  border: 1px solid rgb(var(--color-ink) / 0.055);
  border-radius: var(--radius-paper);
  box-shadow:
    0 14px 36px rgb(0 0 0 / 0.16),
    inset 0 1px 0 rgb(255 255 255 / 0.06);
}
.set__bot {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 4px;
  min-height: 60px;
  position: relative;
  border-radius: var(--radius-seal);
}
.set__bot + .set__bot::before {
  content: "";
  position: absolute;
  top: 0;
  left: 4%;
  right: 4%;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-thread) / 0.22), transparent);
}
.set__bot-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.set__bot-dot.on {
  background: rgb(var(--color-jade));
  box-shadow: 0 0 0 4px rgb(var(--color-jade) / 0.18);
}
.set__bot-dot.off {
  background: rgb(var(--color-ink-soft) / 0.5);
}
.set__bot-info {
  flex: 1;
  min-width: 0;
}
.set__bot-name {
  font-size: 17px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink));
}
.set__bot-meta {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
  margin-top: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ===== themes ===== */
.set__themes {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-top: 4px;
}
.theme-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 16px 8px 12px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.58), rgb(var(--color-bg-veil) / 0.38));
  border: 1px solid rgb(var(--color-ink) / 0.045);
  cursor: pointer;
  border-radius: var(--radius-paper);
  transition:
    background var(--dur-base) ease,
    box-shadow var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap);
  position: relative;
  min-height: 96px;
}
.theme-card:hover {
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.7), rgb(var(--color-bg-veil) / 0.48));
}
.theme-card:active {
  transform: scale(0.97);
}
.theme-card[aria-checked="true"] {
  background: rgb(var(--color-bg-veil) / 0.85);
  box-shadow:
    0 0 0 1px rgb(var(--color-thread) / 0.45),
    0 12px 28px rgb(var(--color-sorrow) / 0.14);
}
/*
 * 选中底纹（朱线）— 始终渲染, 用 scaleX 由两端向中央展开/收回, 切换主题
 * 不再硬切。原写法把 ::after 挂在 [aria-checked="true"] 选择器下, 没有
 * transition 落点, 视觉上是"咔"地一下出现/消失。
 */
.theme-card::after,
.decor-card::after {
  content: "";
  position: absolute;
  bottom: -1px;
  left: 16%;
  right: 16%;
  height: 2px;
  background: linear-gradient(to right, transparent, rgb(var(--color-sorrow)), transparent);
  filter: drop-shadow(0 1px 2px rgb(var(--color-sorrow) / 0.4));
  border-radius: 2px;
  transform: scaleX(0);
  transform-origin: center;
  transition: transform var(--dur-slow) var(--ease-firm);
  pointer-events: none;
}
.theme-card[aria-checked="true"]::after,
.decor-card[aria-checked="true"]::after {
  transform: scaleX(1);
}
.theme-card__sky {
  width: 36px;
  height: 36px;
}
.theme-card__sky svg {
  width: 100%;
  height: 100%;
}
.theme-card__label {
  font-size: 16px;
  letter-spacing: 0.14em;
  color: rgb(var(--color-ink));
}
.theme-card__hint {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft));
}

/* ===== decors ===== */
.set__decors {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-top: 4px;
}
.decor-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 16px 8px 12px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.58), rgb(var(--color-bg-veil) / 0.38));
  border: 1px solid rgb(var(--color-ink) / 0.045);
  cursor: pointer;
  border-radius: var(--radius-paper);
  transition:
    background var(--dur-base) ease,
    box-shadow var(--dur-base) ease,
    transform var(--dur-tap) var(--ease-tap);
  position: relative;
  min-height: 96px;
}
.decor-card:hover {
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.7), rgb(var(--color-bg-veil) / 0.48));
}
.decor-card:active {
  transform: scale(0.97);
}
.decor-card[aria-checked="true"] {
  background: rgb(var(--color-bg-veil) / 0.85);
  box-shadow:
    0 0 0 1px rgb(var(--color-thread) / 0.45),
    0 12px 28px rgb(var(--color-sorrow) / 0.14);
}
.decor-card__art {
  width: 44px;
  height: 28px;
}
.decor-card__art svg {
  width: 100%;
  height: 100%;
}
.decor-card__label {
  font-size: 16px;
  letter-spacing: 0.14em;
  color: rgb(var(--color-ink));
}
.decor-card__hint {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft));
}

/* ===== switches ===== */
.set__switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  background:
    linear-gradient(180deg, rgb(var(--color-bg-veil) / 0.64), rgb(var(--color-bg-veil) / 0.42));
  backdrop-filter: blur(14px);
  border: 1px solid rgb(var(--color-ink) / 0.05);
  border-radius: var(--radius-paper);
  box-shadow:
    0 10px 26px rgb(0 0 0 / 0.13),
    inset 0 1px 0 rgb(255 255 255 / 0.05);
  margin-top: 4px;
}
.set__switch-info {
  flex: 1;
  min-width: 0;
}
.set__switch-name {
  font-size: 16px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink));
}
.set__switch-sub {
  margin-top: 3px;
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-fn);
}

/* ===== about ===== */
.set__about {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding-top: 18px;
  margin-top: 10px;
  position: relative;
  flex-wrap: wrap;
}
.set__about::before {
  content: "";
  position: absolute;
  top: 0;
  left: 10%;
  right: 10%;
  height: 1px;
  background: linear-gradient(to right, transparent, rgb(var(--color-thread) / 0.2), transparent);
}
.set__version {
  font-size: 10px;
  color: rgb(var(--color-ink-soft) / 0.7);
  letter-spacing: 0.18em;
}
.set__version-sub {
  margin-left: 6px;
  letter-spacing: var(--track-fn);
}
.set__logout {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: 0;
  padding: 8px 14px;
  color: rgb(var(--color-alert));
  cursor: pointer;
  border-radius: var(--radius-seal);
  transition:
    color var(--dur-fast) ease,
    transform var(--dur-tap) var(--ease-tap),
    background var(--dur-base) ease;
}
.set__logout:hover {
  color: rgb(var(--color-sorrow));
  background: rgb(var(--color-alert) / 0.08);
}
.set__logout:active {
  transform: scale(0.96);
}
.set__logout-mark {
  font-size: 18px;
  line-height: 1;
  color: rgb(var(--color-alert) / 0.8);
}
.set__logout-label {
  font-size: 16px;
  letter-spacing: 0.14em;
}
.set__logout-hint {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft));
}

@media (max-width: 420px) {
  .set__themes,
  .set__decors {
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }
  .theme-card,
  .decor-card {
    padding: 12px 4px 10px;
    min-height: 88px;
  }
  .theme-card__label,
  .decor-card__label {
    font-size: 14px;
    letter-spacing: 0.18em;
  }
}
</style>
