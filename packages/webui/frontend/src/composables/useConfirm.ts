import { reactive, readonly } from "vue";

/**
 * 全局"焚断红线"二次确认。
 *
 * 不可逆动作 (删除 / 清空 / 退出登录 等) 调 `confirm({...})` 或语义化 preset
 * `confirmDestructive(...)`, 等用户在 `UiConfirmSheet` 里再点一次确认
 * 才 resolve true.
 *
 * 用法:
 *
 *   // 通用
 *   const ok = await confirm({
 *     title: "清空对话",
 *     hint: "清空后内容不可恢复",
 *     confirmLabel: "清空",
 *     tone: "alert",
 *   });
 *
 *   // 不可逆动作的常见场景 — 一行搞定:
 *   if (await confirmDestructive("删除资产", "删除后此项将不再保留。")) {
 *     await deleteKey(...);
 *   }
 *
 *   // 切流 / 换 agent 等"非毁灭性但需要重新开始"的场景:
 *   if (await confirmReroute("切换助手", "当前对话会先清空，再切到新的助手。")) {
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
    state.confirmLabel = req.confirmLabel ?? "确认";
    state.cancelLabel = req.cancelLabel ?? "取消";
    state.tone = req.tone ?? "alert";
    state.open = true;
    state.resolver = resolve;
  });
}

/**
 * 不可逆动作 — alert 调性, 默认确认词"确认"。
 * 用例: 删除 / 清空 / 退出登录.
 */
export function confirmDestructive(
  title: string,
  hint?: string,
  confirmLabel = "确认",
): Promise<boolean> {
  return confirm({ title, hint, confirmLabel, tone: "alert" });
}

/**
 * 改道动作 — thread 调性, 默认确认词"切换"。
 * 用例: 切换助手 / 切换会话场景, 当前进度会被重置。
 */
export function confirmReroute(
  title: string,
  hint?: string,
  confirmLabel = "切换",
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
