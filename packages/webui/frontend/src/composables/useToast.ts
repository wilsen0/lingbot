import { reactive } from "vue";

export type ToastTone = "thread" | "jade" | "alert" | "bell" | "ash";

export interface ToastItem {
  id: number;
  title: string;
  body?: string;
  tone: ToastTone;
}

export const toasts = reactive<ToastItem[]>([]);

let nextId = 1;

/**
 * 推一条 toast. 业务侧建议用 `toast.info / toast.success / toast.error`,
 * 直接调 pushToast 仅在你需要 raw 控制 (例如自定义 tone 或长 ttl) 时用。
 */
export function pushToast(
  title: string,
  body?: string,
  tone: ToastTone = "thread",
  ttlMs = 3600,
): number {
  const id = nextId++;
  toasts.push({ id, title, body, tone });
  setTimeout(() => dismiss(id), ttlMs);
  return id;
}

export function dismiss(id: number): void {
  const idx = toasts.findIndex((t) => t.id === id);
  if (idx >= 0) toasts.splice(idx, 1);
}

/**
 * 语义化 toast API — 替代裸调 pushToast(title, body, "alert"):
 *
 *   toast.success("已存", key)
 *   toast.error("存玉失败", err.message)
 *   toast.info("已切换", scope)
 *   toast.warn("已被他人改写", "请重新打开")
 *
 * 三种语义对应 jade / alert / thread / ash 四个 tone, 调用方不再需要
 * 自己挑色, 减少视觉口径不一致。
 */
export const toast = {
  /** 成功类 — 玉绿点 */
  success(title: string, body?: string): number {
    return pushToast(title, body, "jade");
  },
  /** 错误类 — 朱赭点 */
  error(title: string, body?: string): number {
    return pushToast(title, body, "alert");
  },
  /** 中性提示 — 红线点 */
  info(title: string, body?: string): number {
    return pushToast(title, body, "thread");
  },
  /** 警告但非错误 — 朱赭, 与 error 同色但语义不同 (调用方自己掂量) */
  warn(title: string, body?: string): number {
    return pushToast(title, body, "alert");
  },
  /** 资讯类弱提示 — 灰墨点 */
  hint(title: string, body?: string): number {
    return pushToast(title, body, "ash");
  },
};

/**
 * 把异步操作的"失败 → toast"模板抽出来:
 *
 *   await withToast("存玉失败", () => writeKey(...));
 *
 * 失败时自动推 error toast (含 message). 成功时返回 await 结果, 失败时
 * rethrow 让调用方自己决定要不要继续 (例如设 finally { saving = false }).
 *
 * 你也可以传 onError 拦截特定异常 (例如 412 Precondition Failed):
 *
 *   await withToast("存玉失败", fn, {
 *     onError: (e) => {
 *       if (axios.isAxiosError(e) && e.response?.status === 412) {
 *         toast.warn("已被他人改写", "请重新打开");
 *         return true; // 返回 true 表示已经处理过, 不要再推默认 toast
 *       }
 *       return false;
 *     },
 *   });
 */
export async function withToast<T>(
  errLabel: string,
  fn: () => Promise<T>,
  opts: { onError?: (e: unknown) => boolean | void } = {},
): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    const handled = opts.onError?.(e);
    if (!handled) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(errLabel, msg);
    }
    throw e;
  }
}

export function useToast() {
  return { push: pushToast, dismiss, toasts, toast, withToast };
}
