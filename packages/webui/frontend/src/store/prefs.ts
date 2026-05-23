import { defineStore } from "pinia";
import { ref, watchEffect } from "vue";

export type ThemeMode = "auto" | "light" | "dark";
export type DecorLevel = "full" | "subtle" | "off";

/** 旧版本里 Chat.vue 自己用过的 localStorage key (一次性迁移用). */
const LEGACY_SCOPE_KEY = "linling.chat.scope_id";

/**
 * 读取并清除老版本的 scope localStorage. 仅首次调用返回值非空。
 * 调用方在初始化 scope ref 之前用它做迁移即可:
 *
 *   const scope = ref<string>(readAndConsumeLegacyScope());
 *
 * 之后这条遗留 key 就被清掉, 不会再回写到本 store 之外的地方。
 */
function readAndConsumeLegacyScope(): string {
  if (typeof localStorage === "undefined") return "";
  try {
    const v = localStorage.getItem(LEGACY_SCOPE_KEY);
    if (v) {
      // 立即清掉, 防止下次再读到 (会和 prefs store 的值冲突)
      localStorage.removeItem(LEGACY_SCOPE_KEY);
      return v;
    }
    return "";
  } catch {
    return "";
  }
}

/** 检测当前设备是否为触屏 (coarse pointer). 用于挑首次访问的装饰
 * 默认值 — 手机端默认关掉, 给最稳的视觉体验; 已有用户的 prefs
 * 会被 persistedstate 在 hydrate 阶段覆盖, 不受影响。 */
function isCoarsePointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

/**
 * User UI preferences — 主题、装饰浓度、铃铛音效、震感、测试场景 (scope).
 * 全部由 pinia-plugin-persistedstate 持久化到 localStorage.linling.prefs.
 *
 * 历史: 早期 Chat.vue 自己 try/catch localStorage 实现了 scope, key 是
 * `linling.chat.scope_id`. 重构后并入 prefs store, 第一次加载时做一次性
 * 迁移 — 老用户升级不会丢设置。
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
    /**
     * 测试场景的 QQ scope id. 空字符串 = 默认 (用 dispatcher 的 webui:<user> 合成 scope).
     * 任意数字 (例 "754800438") = 把 WebUI 这条对话当作那个群发出.
     *
     * 注意: 初始值优先用旧版本的 legacy localStorage key (一次性迁移),
     * 之后由 persistedstate 走 linling.prefs key 接管。如果老 key 和 prefs
     * 都有值, persistedstate 会在 hydrate 阶段以 prefs 为准覆盖 — 这是
     * 预期行为, 因为 persisted prefs 一定比 legacy key 更新。
     */
    const scope = ref<string>(readAndConsumeLegacyScope());

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
    function setScope(v: string) {
      scope.value = v.trim();
    }

    return {
      theme,
      decor,
      bellSound,
      haptics,
      scope,
      setTheme,
      setDecor,
      toggleBellSound,
      toggleHaptics,
      setScope,
    };
  },
  {
    persist: {
      key: "linling.prefs",
      storage: typeof localStorage !== "undefined" ? localStorage : undefined,
    },
  },
);
