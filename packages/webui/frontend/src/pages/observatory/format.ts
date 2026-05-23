/**
 * 紧凑时间格式 — 月/日 + 时:分:秒, 跨零点也能区分。
 * 因 events / audit 两边都要, 抽一份共用。
 */
export function formatCompactTime(t: string | number): string {
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
