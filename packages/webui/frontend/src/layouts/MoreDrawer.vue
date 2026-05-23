<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div
        v-if="open"
        class="drawer-root"
        role="dialog"
        aria-modal="true"
        aria-label="菜单"
      >
        <div class="drawer-veil" @click="emit('close')" />
        <aside ref="panelEl" class="drawer-panel px-safe" tabindex="-1">
          <header class="drawer-head">
            <h2 class="drawer-head__name font-display">linling</h2>
            <button class="icon-btn tap" aria-label="关闭菜单" @click="emit('close')">
              <svg viewBox="0 0 24 24" class="icon-btn__ic" fill="none">
                <path
                  d="M6 6L18 18M18 6L6 18"
                  stroke="currentColor"
                  stroke-width="1.4"
                  stroke-linecap="round"
                />
              </svg>
            </button>
          </header>
          <div class="thread-top drawer-hair" />

          <nav class="drawer-nav" aria-label="页面">
            <router-link
              v-for="(item, idx) in navItems"
              :key="item.to"
              :to="item.to"
              class="drawer-item tap"
              :class="{ 'is-first': idx === 0 }"
              @click="emit('close')"
            >
              <span class="drawer-item__glyph font-display">{{ item.glyph }}</span>
              <span class="drawer-item__main">
                <span class="drawer-item__label font-display">{{ item.label }}</span>
                <span class="drawer-item__hint">{{ item.hint }}</span>
              </span>
              <span class="drawer-item__chevron" aria-hidden="true">›</span>
            </router-link>
          </nav>

          <footer class="drawer-foot">
            <span class="drawer-version">v{{ version }}</span>
          </footer>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, toRef } from "vue";

import { useOverlay } from "@/composables/useOverlay";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<(e: "close") => void>();

const panelEl = ref<HTMLElement | null>(null);
const version = __APP_VERSION__;

useOverlay(toRef(props, "open"), panelEl, {
  dismissible: true,
  onClose: () => emit("close"),
});

const navItems = [
  { to: "/",     glyph: "言", label: "对话", hint: "与红娘相对而言" },
  { to: "/观测", glyph: "观", label: "观测", hint: "因缘 · 灵玉 · 命格" },
  { to: "/设置", glyph: "司", label: "司事", hint: "在册 · 装饰 · 解缘" },
];
</script>

<style scoped>
.drawer-root {
  position: fixed;
  inset: 0;
  z-index: 60;
  /*
   * 输入法回正 — 用 padding-bottom 让内部 flex-end (drawer panel
   * 是 absolute 顶/右/下定位的, 不直接受 padding 影响). 抽屉内里
   * 极少触发输入框, 主要是为里面的滚动列表收住底部. 与 UiSheet
   * 保持一致, 避免 transform 双重位移.
   */
  padding-bottom: var(--vv-bottom, 0px);
  transition: padding-bottom var(--dur-base) var(--ease-stand);
}
@media (prefers-reduced-motion: reduce) {
  .drawer-root { transition: none; }
}
.drawer-veil {
  position: absolute;
  inset: 0;
  background: rgb(0 0 0 / 0.45);
  backdrop-filter: blur(3px);
}
.drawer-panel {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(320px, 88vw);
  padding-top: calc(env(safe-area-inset-top, 0) + 18px);
  padding-bottom: calc(env(safe-area-inset-bottom, 0) + 24px);
  background: rgb(var(--color-bg-veil) / 0.96);
  backdrop-filter: blur(22px) saturate(140%);
  -webkit-backdrop-filter: blur(22px) saturate(140%);
  box-shadow: -14px 0 48px rgb(0 0 0 / 0.5);
  display: flex;
  flex-direction: column;
  outline: none;
}
.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px 12px;
}
.drawer-head__name {
  font-size: clamp(22px, 6vw, 26px);
  letter-spacing: var(--track-poem);
  color: rgb(var(--color-ink));
}

.drawer-hair { margin-bottom: 12px; }

.drawer-nav {
  display: flex;
  flex-direction: column;
  padding: 0 24px;
}
.drawer-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 16px;
  align-items: center;
  padding: 18px 0;
  color: rgb(var(--color-ink));
  text-decoration: none;
  min-height: 56px;
  transition: color var(--dur-fast) ease, transform var(--dur-tap) var(--ease-tap);
  position: relative;
}
.drawer-item:active { transform: translateX(2px); }
.drawer-item:hover  { color: rgb(var(--color-sorrow)); }
.drawer-item.router-link-exact-active { color: rgb(var(--color-sorrow)); }
.drawer-item.router-link-exact-active .drawer-item__glyph { color: rgb(var(--color-sorrow)); }

.drawer-item::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 1px;
  background: linear-gradient(
    to right,
    transparent,
    rgb(var(--color-thread) / 0.22),
    transparent
  );
}
.drawer-item.is-first::before { display: none; }

.drawer-item__glyph {
  font-size: 26px;
  letter-spacing: 0;
  color: rgb(var(--color-thread));
  line-height: 1;
  transition: color var(--dur-fast) ease;
}
.drawer-item__main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.drawer-item__label {
  font-size: 17px;
  letter-spacing: 0.22em;
}
.drawer-item__hint {
  font-size: 11px;
  color: rgb(var(--color-ink-soft));
  letter-spacing: var(--track-meta);
}
.drawer-item__chevron {
  color: rgb(var(--color-ink-soft) / 0.5);
  font-size: 20px;
  font-family: var(--font-display);
  line-height: 1;
}

.drawer-foot {
  margin-top: auto;
  padding: 12px 24px 0;
}
.drawer-version {
  font-family: var(--font-mono);
  font-size: 11px;
  color: rgb(var(--color-ink-soft) / 0.7);
  letter-spacing: 0.18em;
}

.drawer-enter-active,
.drawer-leave-active {
  transition: opacity var(--dur-slow) ease;
}
.drawer-enter-active .drawer-panel,
.drawer-leave-active .drawer-panel {
  transition: transform var(--dur-slow) var(--ease-firm);
}
.drawer-enter-from { opacity: 0; }
.drawer-leave-to { opacity: 0; }
.drawer-enter-from .drawer-panel { transform: translateX(100%); }
.drawer-leave-to .drawer-panel { transform: translateX(100%); }
</style>
