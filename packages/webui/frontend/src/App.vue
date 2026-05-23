<template>
  <DecoStage />
  <UiToast />
  <UiConfirmSheet />
  <router-view />
</template>

<script setup lang="ts">
import UiConfirmSheet from "@/components/UiConfirmSheet.vue";
import UiToast from "@/components/UiToast.vue";
import { useViewport } from "@/composables/useViewport";
import DecoStage from "@/decor/DecoStage.vue";
import { usePrefsStore } from "@/store/prefs";

// 让 prefs 在初始化时就写 data-theme（store 里已自动执行）。
usePrefsStore();

// 注册视窗几何追踪: visualViewport 偏移 → :root CSS 变量, 让所有底部锚定
// 的 fixed 元素 (输入条 / sheet / drawer) 直接用 bottom / padding 避让键盘。
useViewport();
</script>
