<template>
  <div class="app-shell">
    <header v-if="showShellBar" class="shell-bar px-safe pt-safe" role="banner">
      <div class="thread-top" />
      <div class="shell-bar__inner">
        <router-link to="/" class="icon-btn tap" aria-label="回到对话">
          <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
            <path
              d="M15 6L9 12L15 18"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </router-link>
        <div class="shell-title-wrap">
          <h1 class="shell-title font-display">{{ shellHead.glyph }}</h1>
          <p class="shell-sub">{{ shellHead.label }}</p>
        </div>
        <button class="icon-btn tap" aria-label="打开菜单" @click="drawerOpen = true">
          <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
            <path d="M4 8h16M4 16h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
        </button>
      </div>
    </header>

    <main class="shell-main" :class="{ 'shell-main--lock': lockScroll }" role="main">
      <router-view v-slot="{ Component }">
        <!--
          mode="out-in" 保证一次只有一份页面挂载, 避免 chat 在切走后还
          短暂持有 WebSocket; 时长用 dur-slow + ease-stand, 与全站统一。
          注意: 内部 page 不要再在根节点跑 --motion-fade-in-up, 否则会
          和这层 transition 叠加成"双重淡入"。
        -->
        <transition name="fade-up" mode="out-in">
          <component :is="Component" @open-drawer="drawerOpen = true" />
        </transition>
      </router-view>
    </main>

    <MoreDrawer :open="drawerOpen" @close="drawerOpen = false" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";

import MoreDrawer from "@/layouts/MoreDrawer.vue";

const route = useRoute();
const shellHead = computed(() => {
  // 路由门头：观测 → 观；设置 → 司
  if (route.path.startsWith("/观测")) return { glyph: "观", label: "观测台" };
  if (route.path.startsWith("/设置")) return { glyph: "司", label: "司事" };
  return { glyph: "言", label: "对话" };
});
const showShellBar = computed(() => Boolean(route.meta?.showBack));
/*
 * 仅在聊天路由锁住 shell-main 滚动 + 文档级滚动 (html/body 也跟着锁,
 * 见 tailwind.css 的 html[data-route="chat"] 选择器).
 *
 * 原因 — iOS Safari 在用户聚焦 textarea 时会沿"最近可滚祖先"链做
 * scrollIntoView. 聊天页 composer 是 fixed, 焦点已经可见, 但 Safari
 * 仍然会去滚任意可滚祖先 (html / body / shell-main / msg-list), 把
 * 对话区视觉上"顶上去". 用户实测能向下滑回去就是这条链在动.
 *
 * 把整条链锁住, Safari 找不到可滚祖先就不会滚 — 唯一保留 msg-list
 * 内部滚动让用户能浏览历史. 其他路由 (Login / Settings / Observatory)
 * 没 fixed 输入条, 不锁.
 */
const lockScroll = computed(() => route.name === "chat");

function resetChatScroll() {
  if (typeof window === "undefined") return;
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  try {
    window.scrollTo(0, 0);
  } catch {
    /* no-op */
  }
}

watch(
  lockScroll,
  (locked) => {
    if (typeof document === "undefined") return;
    if (locked) {
      document.documentElement.dataset.route = "chat";
      resetChatScroll();
    } else if (document.documentElement.dataset.route === "chat") {
      delete document.documentElement.dataset.route;
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  if (typeof document === "undefined") return;
  if (document.documentElement.dataset.route === "chat") {
    delete document.documentElement.dataset.route;
  }
});
const drawerOpen = ref(false);
</script>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  /*
   * 100% 链自 html/body { height: 100% } + body { min-height: 100svh }.
   * 不再写 100dvh / 100vh — dvh 在 iOS 地址栏抖动时会重算, 整页跟着抖.
   * svh 锁在最小可见高度, 整个 app-shell 高度纹丝不动.
   */
  min-height: 100%;
}
.shell-bar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: linear-gradient(
    to bottom,
    rgb(var(--color-bg) / 0.86),
    rgb(var(--color-bg) / 0.55)
  );
  backdrop-filter: blur(18px) saturate(130%);
  -webkit-backdrop-filter: blur(18px) saturate(130%);
}
.shell-bar__inner {
  display: grid;
  grid-template-columns: 44px 1fr 44px;
  align-items: center;
  gap: 8px;
  height: 56px;
  padding: 0 10px;
}
.shell-title-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.shell-title {
  font-size: clamp(22px, 6vw, 26px);
  letter-spacing: var(--track-poem);
  color: rgb(var(--color-ink));
  line-height: 1;
  margin: 0;
}
.shell-sub {
  font-size: 10px;
  letter-spacing: var(--track-fn);
  color: rgb(var(--color-ink-soft));
  line-height: 1;
}

.shell-main {
  flex: 1;
  /*
   * 默认: 可滚动 (Settings / Observatory 等需要 shell 自己滚).
   * 聊天路由叠 .shell-main--lock 锁住 — 见下面注释和 AppShell 脚本中的
   * lockScroll 计算属性.
   */
  overflow-y: auto;
  overscroll-behavior: contain;
  min-height: 0;
}
/*
 * 聊天路由专用: 锁住 shell-main 的滚动.
 *
 * 历来 shell-main 一律可滚. 但聊天页有个特殊情况 — composer 是 position:
 * fixed, iOS Safari 在用户聚焦其内的 textarea 时会沿"最近可滚祖先"链做
 * scrollIntoView, 把 shell-main 滚一段距离, 看起来就是"对话区被向上顶,
 * 下方多出空白". 用户实测能"向下滑回去"—证明就是 shell-main 在被滚.
 *
 * 锁住后, Safari 找不到可滚祖先, 焦点元素 (composer/textarea) 已经
 * 在 fixed 位置可见, 不会被滚. msg-list 仍然内部可滚, 用户的对话浏览
 * 不受影响.
 */
.shell-main--lock {
  overflow: hidden;
}

/*
 * 路由切换 (mode="out-in"):
 *   leave 用 fast 档 — 旧页快速让位, 用户不至于觉得"卡了一下";
 *   enter 用 slow 档 — 新页舒展入场, 与子树自身的入场感保持一致;
 *   transform 距离 6px (原来 10px), 在窄屏上更克制不"晃"。
 */
.fade-up-enter-active {
  transition: opacity var(--dur-slow) var(--ease-stand),
              transform var(--dur-slow) var(--ease-stand);
}
.fade-up-leave-active {
  transition: opacity var(--dur-fast) var(--ease-stand),
              transform var(--dur-fast) var(--ease-stand);
}
.fade-up-enter-from { opacity: 0; transform: translateY(6px); }
.fade-up-leave-to { opacity: 0; transform: translateY(-3px); }
</style>
