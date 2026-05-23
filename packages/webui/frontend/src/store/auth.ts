import { defineStore } from "pinia";
import { computed, ref } from "vue";

/**
 * 登录态。真正的 token 交换 / 刷新由 api/client.ts 包办；
 * 此 store 只负责暴露当前用户与可见 bots 给 UI 层。
 */
export const useAuthStore = defineStore(
  "auth",
  () => {
    const accessToken = ref<string | null>(null);
    const refreshToken = ref<string | null>(null);
    const profile = ref<{
      sub: string;
      role: "superadmin" | "bot_admin" | "readonly";
      bots: string[];
    } | null>(null);

    const isAuthed = computed(() => !!accessToken.value);

    /**
     * 是否有写入权限 — `bot_admin` 或 `superadmin`。
     * `readonly` 角色后端会 403 写入端点（/kv PATCH/DELETE、/bots/.../hot-reload、
     * /rules/files PUT），所以前端用这个计算属性来灰掉/隐藏对应按钮，避免
     * 用户点了才发现没权限。
     */
    const canWrite = computed(() => {
      const r = profile.value?.role;
      return r === "superadmin" || r === "bot_admin";
    });

    function setTokens(access: string, refresh: string) {
      accessToken.value = access;
      refreshToken.value = refresh;
    }
    function setProfile(p: typeof profile.value) {
      profile.value = p;
    }
    function clear() {
      accessToken.value = null;
      refreshToken.value = null;
      profile.value = null;
    }

    return {
      accessToken,
      refreshToken,
      profile,
      isAuthed,
      canWrite,
      setTokens,
      setProfile,
      clear,
    };
  },
  {
    persist: {
      key: "linling.auth",
      storage: typeof localStorage !== "undefined" ? localStorage : undefined,
      pick: ["accessToken", "refreshToken", "profile"],
    },
  },
);
