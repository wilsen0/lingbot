import { defineStore } from "pinia";
import { ref, watchEffect } from "vue";

export type ThemeMode = "auto" | "light" | "dark";
export type DecorLevel = "full" | "subtle" | "off";

/** 检测当前设备是否为触屏 (coarse pointer). 用于挑首次访问的装饰
 * 默认值 — 手机端默认关掉, 给最稳的视觉体验; 已有用户的 prefs
 * 会被 persistedstate 在 hydrate 阶段覆盖, 不受影响。 */
function isCoarsePointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

/**
 * User UI preferences — 主题、装饰浓度、铃铛音效、震感。
 * 全部由 pinia-plugin-persistedstate 持久化到 localStorage.linling.prefs.
 */
export const usePrefsStore = defineStore(
  "prefs",
  () => {
    const theme = ref<ThemeMode>("auto");
    /**
     * 装饰浓度. 桌面默认 subtle, 手机默认 off — 移动端持续跑两块
     * 全屏 canvas 是发热与抖动的主要源, 选择"开"应该是用户主动决定;
     * 用户在设置里改成 subtle/full 后由 persistedstate 持久化下来,
     * 下次访问就走持久化的值。
     */
    const decor = ref<DecorLevel>(isCoarsePointer() ? "off" : "subtle");
    const bellSound = ref(false);
    const haptics = ref(true);

    if (typeof document !== "undefined") {
      watchEffect(() => {
        document.documentElement.dataset.theme = theme.value;
      });
    }

    function setTheme(mode: ThemeMode) {
      theme.value = mode;
    }
    function setDecor(level: DecorLevel) {
      decor.value = level;
    }
    function toggleBellSound() {
      bellSound.value = !bellSound.value;
    }
    function toggleHaptics() {
      haptics.value = !haptics.value;
    }

    return {
      theme,
      decor,
      bellSound,
      haptics,
      setTheme,
      setDecor,
      toggleBellSound,
      toggleHaptics,
    };
  },
  {
    persist: {
      key: "linling.prefs",
      storage: typeof localStorage !== "undefined" ? localStorage : undefined,
    },
  },
);
