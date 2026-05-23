<template>
  <div class="login px-safe pb-safe">
    <div class="login__card">
      <!-- 顶端一枚铃 · 红线吊穗 -->
      <svg class="login__hang" viewBox="0 0 40 88" aria-hidden="true">
        <defs>
          <linearGradient id="lo_bell" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="rgb(var(--color-bell))" />
            <stop offset="100%" stop-color="rgb(var(--color-bell) / .45)" />
          </linearGradient>
        </defs>
        <path
          d="M20 0 C 22 20, 18 44, 22 60"
          stroke="rgb(var(--color-thread) / .8)"
          stroke-width="0.9"
          fill="none"
          stroke-linecap="round"
        />
        <g transform="translate(22 64)" class="login__bell">
          <path
            d="M-8 -2 a 8 6 0 0 1 16 0 v 6 l 1.6 2 h -19.2 l 1.6 -2 z"
            fill="url(#lo_bell)"
          />
          <circle cx="0" cy="9" r="1.8" fill="rgb(var(--color-sorrow))" />
          <path
            d="M-3 10 q 3 7 0 14 M 3 10 q -3 7 0 14"
            stroke="rgb(var(--color-thread))"
            stroke-width="0.9"
            fill="none"
            stroke-linecap="round"
          />
        </g>
      </svg>

      <div class="login__head">
        <h1 class="login__title font-display">linling · 林林</h1>
        <p class="login__sub">— 缘起 —</p>
      </div>

      <!-- 顶部错误条：放在表单上方，避免错把"掌门有误"挂到口诀字段 -->
      <Transition name="fade-slide">
        <div v-if="error" class="login__error" role="alert">
          <span class="login__error-mark" aria-hidden="true">·</span>
          <span class="login__error-text">{{ error }}</span>
        </div>
      </Transition>

      <form class="login__form" novalidate @submit.prevent="onSubmit">
        <UiInput
          v-model="username"
          label="掌门"
          autocomplete="username"
          placeholder="名"
          required
        />
        <UiInput
          v-model="password"
          label="口诀"
          type="password"
          autocomplete="current-password"
          placeholder="讳"
          required
        />
        <button
          type="submit"
          class="login__seal tap"
          :class="{ 'is-loading': loading }"
          :disabled="loading"
          :aria-label="loading ? '正在结缘' : '结缘'"
        >
          <span class="login__seal-frame" aria-hidden="true">
            <svg viewBox="0 0 200 56" preserveAspectRatio="none">
              <!-- 红线沿按钮一圈勾画 -->
              <path
                ref="threadPath"
                d="M 12 28 C 12 12, 24 8, 100 8 C 176 8, 188 12, 188 28 C 188 44, 176 48, 100 48 C 24 48, 12 44, 12 28 Z"
                stroke="rgb(var(--color-bell) / .85)"
                stroke-width="1.4"
                fill="none"
                stroke-linecap="round"
                :stroke-dasharray="threadLen"
                :stroke-dashoffset="threadOffset"
              />
            </svg>
          </span>
          <DecoBellLoader v-if="loading" size="sm" />
          <span v-else class="login__seal-label font-display">結&thinsp;緣</span>
          <!-- 印章式钩 -->
          <span class="login__seal-hook" aria-hidden="true" />
        </button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { getProfile, login as apiLogin } from "@/api/client";
import UiInput from "@/components/UiInput.vue";
import { useStageBus } from "@/composables/useStageBus";
import DecoBellLoader from "@/decor/DecoBellLoader.vue";
import { useAuthStore } from "@/store/auth";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();
const username = ref("");
const password = ref("");
const loading = ref(false);
const error = ref<string | null>(null);
const threadPath = ref<SVGPathElement | null>(null);
const threadLen = ref(420);
const threadOffset = ref(420);
const stage = useStageBus();

// 用户改动账号或口诀后, 之前的错误提示自动消散 — 否则改完密码再点登录,
// 错误条仍杵在那里, 看着像"还是错的"。
watch([username, password], () => {
  if (error.value) error.value = null;
});

onMounted(() => {
  // 初始 path 实际长度
  if (threadPath.value) {
    const len = threadPath.value.getTotalLength();
    threadLen.value = len;
    threadOffset.value = len;
  }
});

async function onSubmit() {
  error.value = null;
  if (!username.value || !password.value) {
    error.value = "请输入掌门与口诀。";
    return;
  }
  loading.value = true;
  // 红线绕按钮一圈：在 loading 时把 offset → 0
  threadOffset.value = 0;
  try {
    const tokens = await apiLogin(username.value, password.value);
    auth.setTokens(tokens.access, tokens.refresh);
    const profile = await getProfile();
    auth.setProfile({ sub: profile.username, role: profile.role, bots: profile.bots });
    stage.ringBell(0); // 结缘成功 · 树上一枚铃响
    const next = (route.query.next as string | undefined) ?? "/";
    await router.replace(next);
  } catch (e: unknown) {
    if (e && typeof e === "object" && "response" in e) {
      const resp = (e as { response?: { status?: number; data?: { detail?: string } } }).response;
      if (resp?.status === 401) error.value = "掌门或口诀有误。";
      else if (resp?.status === 429) error.value = "尝试太频繁 · 稍后再来。";
      else error.value = resp?.data?.detail ?? "未能结缘。";
    } else {
      error.value = e instanceof Error ? e.message : "登录失败";
    }
    // 失败 → 红线退回
    threadOffset.value = threadLen.value;
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.login {
  /* svh 链 — 见 AppShell 同名注释 */
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--pad-x);
  position: relative;
  /*
   * 输入法回正:
   *   登录卡片是垂直居中布局, 键盘弹起后视觉可见区下底被键盘吃掉, 居中点
   *   也下移, 口诀框可能被键盘挡住. 用 padding-bottom 把"可视中心"上移
   *   半个键盘高度 — 与 transform 等价但避开 iOS Safari fixed+transform
   *   的双重位移 quirk.
   */
  padding-bottom: calc(var(--pad-x) + var(--vv-bottom, 0px));
  transition: padding-bottom var(--dur-base) var(--ease-stand);
}
@media (prefers-reduced-motion: reduce) {
  .login { transition: none; }
}
.login__card {
  position: relative;
  width: 100%;
  max-width: 380px;
  display: flex;
  flex-direction: column;
  gap: clamp(18px, 4vw, 28px);
  padding: clamp(36px, 7vw, 52px) clamp(22px, 6vw, 36px) clamp(28px, 6vw, 40px);
  background: rgb(var(--color-bg-veil) / 0.62);
  backdrop-filter: blur(22px) saturate(140%);
  -webkit-backdrop-filter: blur(22px) saturate(140%);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .25),
    0 30px 72px rgb(0 0 0 / .32),
    inset 0 1px 0 rgb(255 255 255 / .08);
}
/* 卡角压痕 */
.login__card::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  width: 18px;
  height: 18px;
  background: linear-gradient(
    135deg,
    rgb(var(--color-thread) / .55) 0%,
    rgb(var(--color-thread) / 0) 70%
  );
  border-top-left-radius: 2px;
  pointer-events: none;
}

.login__hang {
  position: absolute;
  top: -40px;
  right: clamp(20px, 10%, 48px);
  width: clamp(36px, 10vw, 48px);
  height: auto;
  pointer-events: none;
}
.login__bell {
  transform-box: fill-box;
  transform-origin: 0 -10px;
  /* 全站统一的"风中铃"摆动. 与 DecoBellLoader / UiSwitch 的铃用同一支 keyframe / 同一支缓动 */
  animation: var(--motion-bell-swing);
}

.login__head {
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.login__title {
  font-size: clamp(26px, 8vw, 32px);
  letter-spacing: var(--track-poem);
  color: rgb(var(--color-ink));
  text-shadow: 0 2px 6px rgb(0 0 0 / .45);
  line-height: 1.1;
}
.login__sub {
  font-family: var(--font-display);
  letter-spacing: 0.5em;
  color: rgb(var(--color-ink-soft));
  font-size: 13px;
}

/* 错误条 */
.login__error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: rgb(var(--color-alert) / 0.14);
  border-radius: var(--radius-seal);
  color: rgb(var(--color-alert));
  letter-spacing: var(--track-fn);
  font-size: 13px;
}
.login__error-mark {
  font-size: 18px;
  line-height: 1;
}
.login__error-text { flex: 1; min-width: 0; }

.login__form {
  display: flex;
  flex-direction: column;
  gap: clamp(18px, 4vw, 24px);
}

/* 印章式 "结缘" 按钮：朱红印 + 金边红线绕一圈 */
.login__seal {
  position: relative;
  margin-top: 8px;
  min-height: 56px;
  border: 0;
  cursor: pointer;
  background: linear-gradient(
    135deg,
    rgb(var(--color-sorrow)) 0%,
    rgb(var(--color-sakura-2)) 100%
  );
  color: rgb(var(--color-bg));
  border-radius: var(--radius-seal);
  padding: 12px 28px;
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / .14),
    inset 0 -1px 0 rgb(0 0 0 / .14),
    0 4px 14px rgb(var(--color-sorrow) / .26);
  transition:
    transform var(--dur-tap) var(--ease-tap),
    opacity var(--dur-fast) ease,
    filter var(--dur-fast) ease,
    box-shadow var(--dur-base) ease;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.login__seal-frame {
  position: absolute;
  inset: 4px;
  pointer-events: none;
  opacity: 0.85;
}
.login__seal-frame svg {
  width: 100%;
  height: 100%;
}
.login__seal-frame path {
  /*
   * "红线绕印一圈" — 与 UiConfirmSheet 的 confirm-thread 同语义,
   * 都用 var(--dur-stage) + var(--ease-firm), 视觉上是同一种"红线展开"。
   * 原本写 1.6s 太长了, 用户点了"结缘"按钮后红线还没绕完, 体感拖。
   */
  transition: stroke-dashoffset var(--dur-stage) var(--ease-firm);
}
.login__seal.is-loading .login__seal-frame path {
  filter: drop-shadow(0 0 6px rgb(var(--color-bell) / .55));
}
.login__seal-label {
  font-size: 22px;
  letter-spacing: 0.32em;
  line-height: 1;
  position: relative;
  z-index: 1;
}
.login__seal-hook {
  position: absolute;
  right: 14px;
  top: 50%;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--color-bell));
  transform: translateY(-50%);
  box-shadow: 0 0 0 2px rgb(var(--color-bg) / .35);
  pointer-events: none;
}
.login__seal:disabled {
  opacity: 0.7;
  cursor: not-allowed;
}
.login__seal:hover:not(:disabled) {
  filter: brightness(1.06);
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / .18),
    inset 0 -1px 0 rgb(0 0 0 / .14),
    0 6px 18px rgb(var(--color-sorrow) / .36);
}
.login__seal:active:not(:disabled) { transform: scale(0.97); }

/* 错误转场 */
.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: opacity var(--dur-base) ease, transform var(--dur-slow) var(--ease-stand);
}
.fade-slide-enter-from,
.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (max-width: 360px) {
  .login__card { padding: 32px 20px 26px; }
}

@media (prefers-reduced-motion: reduce) {
  .login__seal-frame path {
    transition: none !important;
  }
}
</style>
