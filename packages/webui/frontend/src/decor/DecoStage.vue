<template>
  <!--
    全屏舞台：樱下 · 花落 · 萤起
    层级：sakura (z=0) → petals (z=1) → fireflies (z=1) → app #app (z=2)
    aria-hidden 纯装饰。
  -->
  <div class="deco-stage" aria-hidden="true">
    <DecoSakuraTree />
    <DecoPetals :density="petalDensity" />
    <DecoFireflies :density="fireflyDensity" />
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

import DecoFireflies from "@/decor/DecoFireflies.vue";
import DecoPetals from "@/decor/DecoPetals.vue";
import DecoSakuraTree from "@/decor/DecoSakuraTree.vue";
import { usePrefsStore } from "@/store/prefs";

const prefs = usePrefsStore();
const petalDensity = computed(() => prefs.decor);
const fireflyDensity = computed(() => {
  if (prefs.decor === "full") return "subtle"; // full 时粒子不堆太满
  return prefs.decor;
});
</script>

<style scoped>
.deco-stage {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
}
</style>
