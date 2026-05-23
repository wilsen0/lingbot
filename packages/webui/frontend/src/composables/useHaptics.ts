import { usePrefsStore } from "@/store/prefs";

/**
 * `useHaptics()` — fires a short vibration on touch devices, gated by
 * `prefs.haptics` preference. No-op on desktop or where the API is absent.
 */
export function useHaptics() {
  function tap(ms = 15): void {
    const prefs = usePrefsStore();
    if (!prefs.haptics) return;
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    if (!nav || typeof nav.vibrate !== "function") return;
    try {
      nav.vibrate(ms);
    } catch {
      /* no-op */
    }
  }
  function pattern(pattern: number[]): void {
    const prefs = usePrefsStore();
    if (!prefs.haptics) return;
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    if (!nav || typeof nav.vibrate !== "function") return;
    try {
      nav.vibrate(pattern);
    } catch {
      /* no-op */
    }
  }
  return { tap, pattern };
}
