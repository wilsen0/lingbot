import { reactive, readonly } from "vue";

/**
 * 全局"焚断红线"二次确认。
 *
 * 不可逆动作 (解缘 / 焚此缘 / 去玉 等) 调 `confirm({...})` 或语义化 preset
 * `confirmDestructive(...)`, 等用户在 `UiConfirmSheet` 里再点一次"以墨为誓"
 * 才 resolve true.
 *
 * 用法:
 *
 *   // 通用
 *   const ok = await confirm({
 *     title: "焚此缘",
 *     hint: "焚后此对话不复",
 *     confirmLabel: "焚",
 *     tone: "alert",
 *   });
 *
 *   // 不可逆动作的常见场景 — 一行搞定:
 *   if (await confirmDestructive("焚此玉", "焚后此键不复。")) {
 *     await deleteKey(...);
 *   }
 *
 *   // 切流 / 换 agent 等"非毁灭性但需要重新开始"的场景:
 *   if (await confirmReroute("另结一段", "此对话将焚于风中。")) {
 *     ...
 *   }
 */

export type ConfirmTone = "alert" | "thread" | "ash";

export interface ConfirmRequest {
  title: string;
  hint?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: ConfirmTone;
}

interface ConfirmState extends ConfirmRequest {
  open: boolean;
  resolver: ((v: boolean) => void) | null;
}

const state = reactive<ConfirmState>({
  open: false,
  title: "",
  hint: undefined,
  confirmLabel: undefined,
  cancelLabel: undefined,
  tone: undefined,
  resolver: null,
});

export function confirm(req: ConfirmRequest): Promise<boolean> {
  return new Promise((resolve) => {
    if (state.resolver) {
      // 已有未决确认 — 拒绝新的
      resolve(false);
      return;
    }
    state.title = req.title;
    state.hint = req.hint;
    state.confirmLabel = req.confirmLabel ?? "焚";
    state.cancelLabel = req.cancelLabel ?? "回";
    state.tone = req.tone ?? "alert";
    state.open = true;
    state.resolver = resolve;
  });
}

/**
 * 不可逆毁灭性动作 — 朱赭 alert 调性, 默认确认词"焚"。
 * 用例: 解缘登出 / 焚此缘清空对话 / 焚此玉删 KV.
 */
export function confirmDestructive(
  title: string,
  hint?: string,
  confirmLabel = "焚",
): Promise<boolean> {
  return confirm({ title, hint, confirmLabel, tone: "alert" });
}

/**
 * 改道动作 — 朱粉 thread 调性, 默认确认词"另结"。
 * 用例: 切换 agent / 切换会话场景, 当前进度被丢弃但语义不是"毁灭"。
 */
export function confirmReroute(
  title: string,
  hint?: string,
  confirmLabel = "另结",
): Promise<boolean> {
  return confirm({ title, hint, confirmLabel, tone: "thread" });
}

export function _resolveConfirm(v: boolean): void {
  const r = state.resolver;
  state.open = false;
  state.resolver = null;
  if (r) r(v);
}

export const confirmState = readonly(state);
