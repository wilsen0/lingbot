import { usePrefsStore } from "@/store/prefs";

/**
 * `useBellSound()` — plays a soft bell ting. Gated by `prefs.bellSound`.
 * Respects `prefers-reduced-motion` (silent in that mode).
 */
export function useBellSound() {
  let ctx: AudioContext | null = null;

  function reducedMotion(): boolean {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function ensureCtx(): AudioContext | null {
    if (typeof window === "undefined") return null;
    const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return null;
    if (!ctx) ctx = new AC();
    return ctx;
  }

  function ring(freq = 1080): void {
    const prefs = usePrefsStore();
    if (!prefs.bellSound) return;
    if (reducedMotion()) return;
    const ac = ensureCtx();
    if (!ac) return;

    try {
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, ac.currentTime);
      osc.frequency.exponentialRampToValueAtTime(freq * 0.65, ac.currentTime + 0.35);
      gain.gain.setValueAtTime(0.0001, ac.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.12, ac.currentTime + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.5);
      osc.connect(gain).connect(ac.destination);
      osc.start();
      osc.stop(ac.currentTime + 0.55);
    } catch {
      /* no-op */
    }
  }

  return { ring };
}
