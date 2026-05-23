<template>
  <footer ref="dockEl" class="composer px-safe">
    <svg
      class="composer__thread"
      viewBox="0 0 200 20"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path
        d="M0 0 C 40 12, 80 14, 100 18 C 120 14, 160 12, 200 0"
        stroke="rgb(var(--color-thread) / .55)"
        stroke-width="0.6"
        fill="none"
      />
    </svg>

    <div class="composer__stack">
      <ChatComposerSuggest
        :visible="suggest.visible.value"
        :results="suggest.results.value"
        :cursor="suggest.cursor.value"
        :query="suggest.query.value"
        @pick="onPickSuggestion"
        @hover="(idx) => (suggest.cursor.value = idx)"
      />
      <form class="composer__form" @submit.prevent="onSubmit">
        <textarea
          ref="inputEl"
          v-model="draft"
          rows="1"
          :placeholder="placeholder"
          class="composer__input"
          :disabled="disabled"
          :aria-disabled="disabled"
          aria-autocomplete="list"
          :aria-expanded="suggest.visible.value"
          autocomplete="off"
          autocorrect="off"
          autocapitalize="off"
          spellcheck="false"
          @keydown="onKeydown"
          @blur="onBlur"
          @focus="onFocus"
          @compositionstart="composing = true"
          @compositionend="onCompositionEnd"
          @input="onInput"
        />

      <button
        v-if="streaming"
        class="composer__btn composer__btn--stop tap"
        type="button"
        aria-label="停"
        @click="$emit('cancel')"
      >
        <svg viewBox="0 0 24 24" class="composer__btn-stop-ic" fill="none">
          <rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" />
        </svg>
      </button>
      <button
        v-else
        class="composer__btn composer__btn--send tap"
        :class="{ 'is-mute': !canSend }"
        type="submit"
        aria-label="寄一句"
        :disabled="!canSend"
      >
        <!-- 铃铛 · 红线吊穗。无草稿时去掉铃舌 + 飘带 ("铃不响") -->
        <svg viewBox="0 0 36 44" class="composer__btn-send-ic" fill="none" aria-hidden="true">
          <path
            d="M18 2 v 6"
            stroke="rgb(var(--color-thread))"
            stroke-width="1.2"
            stroke-linecap="round"
          />
          <path
            d="M18 8 c -6 0 -10 4 -10 10 v 7 l -2 2 c -0.6 0.6 -0.2 1.6 0.7 1.6 h 22.6 c 0.9 0 1.3 -1 0.7 -1.6 L 28 25 v -7 c 0 -6 -4 -10 -10 -10 z"
            fill="rgb(var(--color-bell))"
            stroke="rgb(var(--color-ink) / .4)"
            stroke-width="0.8"
          />
          <circle v-if="canSend" cx="18" cy="31" r="2.4" fill="rgb(var(--color-sorrow))" />
          <path
            v-if="canSend"
            d="M14 32 q 4 8 0 10 M 22 32 q -4 8 0 10"
            stroke="rgb(var(--color-thread))"
            stroke-width="1.1"
            fill="none"
            stroke-linecap="round"
          />
        </svg>
      </button>
    </form>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import type { TriggerSuggestion } from "@/api/agents";
import { useElementSizeVar } from "@/composables/useElementSizeVar";
import { useRafSchedule } from "@/composables/useRafSchedule";

import ChatComposerSuggest from "./ChatComposerSuggest.vue";
import { lastQueryToken, useTriggerSuggest } from "./useTriggerSuggest";

const props = defineProps<{
  /** 是否禁用 (无可用 agent 时) */
  disabled: boolean;
  /** 是否正在等回复 — true 时按钮变"停" */
  streaming: boolean;
  /** 输入框 placeholder */
  placeholder: string;
  /** 当前 agent 名 — 用来拉取该 bot 可触发的 DSL 候选指令.
   *  null 时候选面板不显示. */
  agentName?: string | null;
}>();

const emit = defineEmits<{
  (e: "submit", text: string): void;
  (e: "cancel"): void;
  (e: "focus"): void;
}>();

const dockEl = ref<HTMLElement | null>(null);
const inputEl = ref<HTMLTextAreaElement | null>(null);
const draft = ref("");
const composing = ref(false);
const keyboardSettleTimers: number[] = [];

/* === 候选指令 (inline-suggest) === */
const suggest = useTriggerSuggest();

/* 切换 agent → 重新拉触发器列表. agentName 为 null 时清空.
 * 不在挂载时一次性拉, 而是 watch 立即触发 (immediate: true) — 这样
 * 父组件初始化时把 agentName 一起传进来也能拿到首发数据. */
watch(
  () => props.agentName ?? null,
  (name) => {
    void suggest.load(name);
  },
  { immediate: true },
);

/* 输入流时不显示候选 — 用户在等回复, 弹一层让 UI 噪声更大没必要.
 * disabled 同理 (没 agent 时也没意义). */
watch(
  () => props.streaming || props.disabled || composing.value,
  (now) => {
    suggest.suppressed.value = now;
  },
  { immediate: true },
);

/**
 * dock 高度 → CSS var --chat-dock-h, 让消息列表自己决定 padding-bottom.
 * 这里不再持有 dockHeight ref / 不绑 :style — 长度信息属于 CSS。
 */
useElementSizeVar(dockEl, "--chat-dock-h", { kind: "height", min: 72 });

const canSend = computed(
  () => !props.disabled && !!draft.value.trim() && !props.streaming,
);

function suggestGuarded(): boolean {
  return props.streaming || props.disabled || composing.value;
}

/**
 * textarea 自动高度. 合并到下一帧, 避免 input → autosize 这条链上的
 * 多次 reflow (max 4 次/输入字符, 一帧仅算 1 次)。
 */
const autosize = useRafSchedule(() => {
  const el = inputEl.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}).trigger;

watch(draft, autosize);

function onKeydown(ev: KeyboardEvent) {
  // 候选面板可见时优先处理导航键. IME 选词期间一切让位 (composing/229).
  const imeActive =
    ev.isComposing ||
    composing.value ||
    (ev as unknown as { keyCode: number }).keyCode === 229;
  if (imeActive) return;

  if (suggest.visible.value) {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      suggest.moveCursor(1);
      return;
    }
    if (ev.key === "ArrowUp") {
      ev.preventDefault();
      suggest.moveCursor(-1);
      return;
    }
    if (ev.key === "Tab") {
      // Tab = "把光标定到候选位置, 但不寄". 让用户继续编辑参数.
      ev.preventDefault();
      const item = suggest.active.value;
      if (item) applySuggestion(item, { autoSend: false });
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      // Esc 一次只关面板, 不清草稿. 再按交给浏览器 (可能触发上层逻辑).
      suggest.suppressed.value = true;
      // 用户下一次输入再恢复.
      return;
    }
    if (ev.key === "Enter" && !ev.shiftKey) {
      // 仅在用户明确高亮 (按过 ↑/↓) 时, Enter 才接受候选.
      // 否则就按原文寄 — 避免"我打的就是我要寄的, 别替我猜"的反直觉.
      // Tab 仍然能在没高亮的情况下接 results[0] (经由 active 兜底).
      if (suggest.cursor.value !== -1) {
        ev.preventDefault();
        const item = suggest.active.value;
        if (item) {
          applySuggestion(item, { autoSend: !item.has_args });
          return;
        }
      }
      // 落到下面默认 Enter 路径 (普通 submit).
    }
  }

  if (ev.key !== "Enter") return;
  if (ev.shiftKey) return;
  ev.preventDefault();
  submit();
}

function onCompositionEnd() {
  composing.value = false;
  // composition 结束的瞬间 input 事件已经发过了 — 但 query 取的是
  // 当时 composing=true 的草稿, 候选过滤会被压制, 所以这里手动同步一下.
  syncQuery();
}

function onInput() {
  autosize();
  // Esc 只是临时收起候选；用户继续输入时恢复，仍受 streaming/disabled/IME 守门。
  if (!suggestGuarded()) suggest.suppressed.value = false;
  syncQuery();
}

function onFocus() {
  // 重新进入输入 → 上次 Esc 隐藏的面板恢复. 仍受 streaming/disabled 守门.
  suggest.suppressed.value = suggestGuarded();
  syncQuery();
  settleOuterScroll();
  emit("focus");
}

function onBlur() {
  // 失焦时关面板, 但 mousedown 选项时已经 preventDefault, blur 不会触发.
  // 这里只是为了让 Tab 键到别的可聚焦元素 / 切窗后视图干净.
  suggest.suppressed.value = true;
}

function syncQuery() {
  // composing 中不更新 query — IME 半成品文本进来会让面板乱跳.
  if (composing.value) {
    suggest.query.value = "";
    return;
  }
  suggest.query.value = lastQueryToken(draft.value);
}

/** 把候选指令应用进草稿: 替换最后一个 token 为 literal_prefix.
 *
 * 设计考量:
 *
 * - 不直接寄出 raw 触发器 (regex 形式). 用户的输入框里出现 ``反馈丢失(.*)``
 *   会很怪, 也不会被 classifier 真的匹配 (regex 字面量本身不是 message).
 *   只填 literal_prefix, 让用户的输入实际就是 bot 看到的文本.
 *
 * - 有参触发器: 不自动寄, 把光标停在末尾, 让用户继续敲参数.
 * - 无参触发器: 直接 submit() — 多敲一下回车体验不好, 也是 TUI 习惯.
 *
 * - 多行草稿: 只替换最后一行的最后一个 token. 其他行保持原样
 *   (用户可能在前几行写了上下文, 忽略它会失礼).
 */
function applySuggestion(
  item: TriggerSuggestion,
  opts: { autoSend: boolean },
): void {
  const before = draft.value;
  const lines = before.split(/\r?\n/);
  const lastLine = lines.at(-1) ?? "";
  // 找到最后一个非空白 token 的起点; lastQueryToken 已经验证过了.
  const m = lastLine.match(/(\S+)$/);
  const tokenStart = m ? lastLine.length - m[1].length : lastLine.length;
  const newLastLine = lastLine.slice(0, tokenStart) + item.literal_prefix;
  lines[lines.length - 1] = newLastLine;
  draft.value = lines.join("\n");
  // 清当前 query — 替换后的尾 token 就是 literal_prefix 本身,
  // 如果还让面板根据它再过滤会再弹一次"自己=自己"的候选, 看着烦.
  // syncQuery 在 nextTick 触发 input/keydown 还会重算, 这里先清.
  suggest.query.value = "";

  void nextTick(() => {
    autosize();
    const el = inputEl.value;
    if (el) {
      const cursor = draft.value.length;
      el.setSelectionRange(cursor, cursor);
      focusInput();
    }
    if (opts.autoSend) {
      submit();
    }
  });
}

function onPickSuggestion(item: TriggerSuggestion) {
  applySuggestion(item, { autoSend: !item.has_args });
}

function onSubmit() {
  submit();
}

function submit() {
  if (!canSend.value) return;
  const text = draft.value.trim();
  if (!text) return;
  emit("submit", text);
  draft.value = "";
  clearSuggest();
  autosize();
}

function clearSuggest() {
  suggest.query.value = "";
  suggest.cursor.value = -1;
}

function resetOuterScroll() {
  if (typeof window === "undefined") return;
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  const shellMain = document.querySelector<HTMLElement>(".shell-main");
  if (shellMain) shellMain.scrollTop = 0;
  try {
    window.scrollTo(0, 0);
  } catch {
    /* jsdom / locked browsers may not expose scrollTo */
  }
}

function settleOuterScroll() {
  if (typeof window === "undefined") return;
  for (const id of keyboardSettleTimers) window.clearTimeout(id);
  keyboardSettleTimers.length = 0;
  resetOuterScroll();
  window.requestAnimationFrame(resetOuterScroll);
  keyboardSettleTimers.push(
    window.setTimeout(resetOuterScroll, 80),
    window.setTimeout(resetOuterScroll, 320),
  );
}

function focusInput() {
  inputEl.value?.focus({ preventScroll: true });
  settleOuterScroll();
}

onBeforeUnmount(() => {
  for (const id of keyboardSettleTimers) window.clearTimeout(id);
  keyboardSettleTimers.length = 0;
});

defineExpose({
  /** 父组件可主动聚焦 (例如 / 快捷键) */
  focus: focusInput,
  /** 父组件清空草稿 — 焚此缘 / 切换 agent 时 */
  clear: () => {
    draft.value = "";
    clearSuggest();
    autosize();
  },
});
</script>

<style scoped>
.composer {
  position: fixed;
  left: 0;
  right: 0;
  /*
   * 直接用 bottom 锚定到键盘上沿. 早先用 transform: translateY(-vv-bottom)
   * 抬起 — 在 iOS Safari 与 position:fixed 组合时 transform 不靠谱:
   *
   *   • iOS Safari 在键盘弹起时会自动 scrollIntoView 焦点元素, 这会
   *     滚动 layout viewport, 而 fixed 元素跟着 layout 滚 (iOS quirk).
   *     然后我们再 transform 抬一下, 双重位移产生肉眼可见的 ~80px+
   *     空白 — 即用户截图里看到的那块.
   *   • bottom 是 layout property, 浏览器在合成键盘动画时知道把 fixed
   *     元素的 bottom 锚点跟着 visualViewport 一起移; 不会双重位移.
   *
   * --vv-bottom 由 useViewport 写入, 默认 0px. 桌面 / 没键盘时 composer
   * 就贴在视口底, 加上 padding-bottom 让出 home indicator.
   */
  bottom: var(--vv-bottom, 0px);
  z-index: 10;
  /*
   * 默认: 让出 iPhone home indicator 安全区 + 12px 呼吸 padding.
   * 键盘开时 home indicator 被键盘盖住, 让位失去意义, 此时 padding 会
   * 被 :root[data-keyboard="open"] 收紧 (见下).
   */
  padding-bottom: calc(env(safe-area-inset-bottom, 0) + 12px);
  padding-top: 8px;
  background: linear-gradient(
    to top,
    rgb(var(--color-bg)) 0%,
    rgb(var(--color-bg) / .9) 30%,
    rgb(var(--color-bg) / .2) 78%,
    rgb(var(--color-bg) / 0)
  );
  transition: bottom var(--dur-base) var(--ease-stand);
}
/*
 * 键盘开 → 收紧 composer 的留白:
 *
 *   • padding-bottom 不让 home indicator 安全区 (键盘已经盖住它了),
 *     只保留 4px 让 form 不贴键盘上沿
 *   • padding-top 也收紧到 2px — 用户的焦点完全在输入框
 *   • 装饰飘带 (__thread SVG) 在窄高度下没意义还占 16px, 直接隐
 */
:root[data-keyboard="open"] .composer {
  padding-bottom: 4px;
  padding-top: 2px;
}
:root[data-keyboard="open"] .composer__thread {
  display: none;
}
@media (prefers-reduced-motion: reduce) {
  .composer { transition: none; }
}

.composer__thread {
  display: block;
  width: min(62%, 400px);
  height: 16px;
  margin: 0 auto -6px;
  opacity: 0.85;
}
.composer__stack {
  position: relative;
  /* 容纳建议面板的浮动 anchor — 面板用 position: absolute, bottom: 100% 贴到 form 上沿. */
}
.composer__form {
  margin: 0 auto;
  max-width: 720px;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 6px 6px 6px 18px;
  background: rgb(var(--color-bg-veil) / 0.86);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border-radius: var(--radius-paper);
  box-shadow:
    0 1px 2px rgb(0 0 0 / .25),
    0 20px 42px rgb(0 0 0 / .18),
    inset 0 1px 0 rgb(255 255 255 / .1);
  margin-inline: var(--pad-x);
}
.composer__input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: 0;
  outline: none;
  resize: none;
  color: rgb(var(--color-ink));
  font-family: var(--font-sans);
  /*
   * 16px = iOS Safari "聚焦时不自动放大" 的下限. 低于此值 layout viewport
   * 会被强行撑大 — 用户必须手动捏合还原, 即"被输入法顶乱"的根因.
   * 见 UiInput 同名注释; 全站所有 input/textarea 都按这条规则。
   */
  font-size: 16px;
  line-height: 1.65;
  letter-spacing: 0.04em;
  padding: 10px 2px;
  max-height: 180px;
}
.composer__input::placeholder {
  color: rgb(var(--color-ink-soft) / 0.7);
  font-family: var(--font-display);
  letter-spacing: 0.4em;
}
.composer__input:disabled {
  color: rgb(var(--color-ink-soft));
  cursor: not-allowed;
}

.composer__btn {
  flex-shrink: 0;
  width: 48px;
  height: 48px;
  border: 0;
  background: transparent;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  position: relative;
  transition: transform var(--dur-tap) var(--ease-tap),
              filter var(--dur-fast) ease,
              opacity var(--dur-fast) ease;
}
.composer__btn:disabled { cursor: not-allowed; }

.composer__btn--send {
  color: rgb(var(--color-bell));
}
.composer__btn-send-ic {
  width: 32px;
  height: 40px;
  filter: drop-shadow(0 2px 6px rgb(var(--color-sorrow) / .35));
  transition: transform var(--dur-fast) var(--ease-tap),
              filter var(--dur-base) ease;
}
.composer__btn--send.is-mute .composer__btn-send-ic {
  filter: drop-shadow(0 2px 4px rgb(0 0 0 / .15)) grayscale(0.6);
  opacity: 0.5;
}
.composer__btn--send:hover:not(:disabled) .composer__btn-send-ic {
  transform: rotate(-8deg);
  filter: drop-shadow(0 4px 12px rgb(var(--color-sorrow) / .55));
}
.composer__btn--send:active:not(:disabled) .composer__btn-send-ic {
  transform: rotate(8deg) scale(0.94);
}

.composer__btn--stop {
  color: rgb(var(--color-ink) / 0.85);
}
.composer__btn--stop:hover { color: rgb(var(--color-ink)); }
.composer__btn-stop-ic { width: 20px; height: 20px; }

@media (max-width: 420px) {
  /* 窄屏稍收紧默认底 padding; 键盘开时仍然走 :root[data-keyboard]
   * 的覆盖规则 (specificity 更高), 不会被这条压回去. */
  .composer { padding-bottom: calc(env(safe-area-inset-bottom, 0) + 8px); }
}
</style>
