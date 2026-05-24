<template>
  <div class="app-shell">
    <transition name="shell-chrome" appear>
      <nav
        v-if="showShellChrome"
        class="shell-chrome px-safe pt-safe"
        aria-label="页面快捷操作"
      >
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
        <button class="icon-btn tap" aria-label="打开菜单" @click="drawerOpen = true">
          <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
            <path
              d="M4 8h16M4 16h10"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linecap="round"
            />
          </svg>
        </button>
      </nav>
    </transition>

    <main class="shell-main" role="main">
      <router-view v-slot="{ Component }">
        <!--
          页层切换不再 out-in: 旧层留在底下淡出, 新层从上方轻落。
          这样从观测 / 设置回对话时, 画面不会先空一帧再挂聊天页。
        -->
        <transition name="page-layer" appear>
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
const showShellChrome = computed(() => Boolean(route.meta?.showBack));
/*
 * 仅在聊天路由锁住文档级滚动 (html/body, 见 tailwind.css 的
 * html[data-route="chat"] 选择器).
 *
 * 原因 — iOS Safari 在用户聚焦 textarea 时会沿"最近可滚祖先"链做
 * scrollIntoView. 聊天页 composer 是 fixed, 焦点已经可见, 但 Safari
 * 仍然会去滚任意可滚祖先 (html / body / msg-list), 把对话区视觉上
 * "顶上去". 用户实测能向下滑回去就是这条链在动.
 *
 * AppShell 现在只是页层舞台, 不再滚动; 这里继续锁住文档级滚动,
 * 唯一保留 msg-list 内部滚动让用户能浏览历史. 其他路由没有 fixed
 * 输入条, 不锁.
 */
const isChatRoute = computed(() => route.name === "chat");

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
  isChatRoute,
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
  position: relative;
  /*
   * 100% 链自 html/body { height: 100% } + body { min-height: 100svh }.
   * 不再写 100dvh / 100vh — dvh 在 iOS 地址栏抖动时会重算, 整页跟着抖.
   * svh 锁在最小可见高度, 整个 app-shell 高度纹丝不动.
  */
  min-height: 100%;
}

.shell-chrome {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 20;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  pointer-events: none;
}
.shell-chrome .icon-btn {
  margin: 10px;
  pointer-events: auto;
  background: rgb(var(--color-bg-veil) / 0.38);
  border: 1px solid rgb(var(--color-ink) / 0.045);
  box-shadow:
    0 10px 28px rgb(0 0 0 / 0.1),
    inset 0 1px 0 rgb(255 255 255 / 0.08);
}

.shell-main {
  flex: 1;
  position: relative;
  overflow: hidden;
  isolation: isolate;
  /*
   * 页层自己持有滚动 (Observatory / Settings), shell-main 只做舞台。
   * 这样 route transition 能把旧页留在底层淡出, 不再依赖外层滚动容器
   * 的高度重排。
   */
  overscroll-behavior: contain;
  min-height: 0;
}
.shell-chrome-enter-active,
.shell-chrome-leave-active {
  transition:
    opacity var(--dur-base) var(--ease-stand),
    transform var(--dur-base) var(--ease-stand);
  will-change: opacity;
}
.shell-chrome-enter-from,
.shell-chrome-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
.shell-chrome-leave-active {
  position: absolute;
  inset: 0 0 auto 0;
  pointer-events: none;
}
@media (prefers-reduced-motion: reduce) {
  .shell-chrome-enter-active,
  .shell-chrome-leave-active {
    transition: none;
  }
}

/*
 * 页层切换: 不走 out-in, 新旧页同时存在一小段时间。
 * 旧页在底层轻微失焦, 新页从更近的层进入, 避免顶部栏/输入框硬切。
 */
.page-layer-enter-active,
.page-layer-leave-active {
  position: absolute;
  inset: 0;
  width: 100%;
  transition:
    opacity var(--dur-slow) var(--ease-stand),
    transform var(--dur-slow) var(--ease-stand),
    filter var(--dur-slow) var(--ease-stand);
  will-change: opacity, transform, filter;
}
.page-layer-enter-active {
  z-index: 1;
}
.page-layer-leave-active {
  z-index: 0;
  pointer-events: none;
}
.page-layer-enter-from {
  opacity: 0;
  filter: blur(8px);
  transform: translateY(10px);
}
.page-layer-leave-to {
  opacity: 0;
  filter: blur(4px);
  transform: translateY(-4px);
}
@media (prefers-reduced-motion: reduce) {
  .page-layer-enter-active,
  .page-layer-leave-active {
    transition: none;
    filter: none;
    transform: none;
  }
}
</style>
