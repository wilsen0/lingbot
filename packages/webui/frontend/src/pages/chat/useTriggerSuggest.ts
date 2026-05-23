import { computed, ref, shallowRef, watch, type Ref } from "vue";

import { listTriggers, type TriggerSuggestion } from "@/api/agents";

/**
 * Inline-suggest controller for the chat composer.
 *
 * Loads the agent's matchable DSL triggers once per agent open, then
 * ranks/filters them client-side as the user types. Kept separate
 * from ``ChatComposer.vue`` so the ranking logic can be unit-tested
 * and so the composer's layout/IME concerns stay focused.
 *
 * Design notes:
 *
 * - Server poll is **per-agent open**, not per-keystroke. The bot's
 *   trigger list is small (<500 entries even for the QRDic-migrated
 *   ruleset) and changes only on hot-reload; client-side filtering
 *   over a small array is faster than a round-trip per character.
 *   Hot-reload picks up on the next agent switch / page reload.
 *
 * - Ranking honours three tiers: exact prefix > word boundary > any
 *   substring. CJK has no word boundaries in the western sense, so
 *   the "word boundary" tier degrades to "literal_prefix prefix"
 *   which still matches the way QRDic users think (``反馈丢失`` →
 *   typing ``反馈`` ranks ``反馈丢失…`` above ``丢失反馈…``).
 *
 * - The query is the **last whitespace-separated token** of the
 *   draft. That keeps the panel useful when the user has already
 *   typed a parametric trigger's prefix and is now typing args
 *   (``反馈丢失 思思`` → no match needed; query degrades to ``思思``
 *   which won't match a trigger so the panel hides — exactly what
 *   we want).
 */

/** Maximum number of suggestions to surface in one panel. Beyond
 * this, the user will type more characters anyway. The cap also
 * keeps the rendering off the main thread. */
const MAX_RESULTS = 8;

/** Minimum query length before we surface anything. ``1`` is the
 * obvious choice — a single CJK char already narrows ~95% of
 * triggers — but ASCII users will see noise at length 1, so we
 * bump to ``2`` for ASCII-only queries. */
function minQueryLen(query: string): number {
  // Any non-ASCII character (CJK, kana, kanji, etc.) → ``1``.
  return /[^\x00-\x7f]/.test(query) ? 1 : 2;
}

interface RankedSuggestion extends TriggerSuggestion {
  /** Lower is better — used internally for stable sort. */
  _score: number;
}

function score(query: string, item: TriggerSuggestion): number {
  if (!query) return Number.POSITIVE_INFINITY;
  const q = query.toLowerCase();
  const label = item.label.toLowerCase();
  const prefix = item.literal_prefix.toLowerCase();

  // Exact match wins.
  if (label === q || prefix === q) return 0;
  // Prefix match on literal_prefix — this is the "intended path"
  // for parametric triggers.
  if (prefix.startsWith(q)) return 1;
  // Prefix match on full label — covers no-arg literal triggers.
  if (label.startsWith(q)) return 2;
  // Substring fallback. We deliberately don't fuzzy-match (no
  // levenshtein, no skip-char) — QRDic triggers are short Chinese
  // phrases and false positives there are worse than missing a few.
  const idx = label.indexOf(q);
  if (idx >= 0) return 3 + idx; // earlier substring → better
  return Number.POSITIVE_INFINITY;
}

export interface UseTriggerSuggest {
  /** All triggers loaded from the server. ``shallowRef`` because the
   * array contents are immutable from our perspective. */
  all: Readonly<Ref<TriggerSuggestion[]>>;
  /** Filtered, ranked, capped to ``MAX_RESULTS``. */
  results: Readonly<Ref<TriggerSuggestion[]>>;
  /** Set by the composer; the last whitespace-separated token of
   * the draft. */
  query: Ref<string>;
  /** Set externally to suppress the panel (streaming, IME, blur). */
  suppressed: Ref<boolean>;
  /** True iff we should render the panel right now. */
  visible: Readonly<Ref<boolean>>;
  /** Currently highlighted index inside ``results``. ``-1`` when
   * nothing is highlighted (panel just opened, user hasn't pressed
   * arrow keys yet). */
  cursor: Ref<number>;
  /** Fetch triggers for ``agentName``. Idempotent within one agent —
   * a re-call clears + reloads. Pass ``null`` to clear everything
   * (e.g. on agent close). */
  load: (agentName: string | null) => Promise<void>;
  /** Move cursor by ``delta``. Wraps. */
  moveCursor: (delta: number) => void;
  /** The currently highlighted suggestion, or null. */
  active: Readonly<Ref<TriggerSuggestion | null>>;
}

export function useTriggerSuggest(): UseTriggerSuggest {
  const all = shallowRef<TriggerSuggestion[]>([]);
  const query = ref("");
  const suppressed = ref(false);
  const cursor = ref(-1);
  /** Tracks the in-flight load so a fast switch (agent A → B → A)
   * doesn't accept B's late response into A's panel. */
  let loadToken = 0;

  async function load(agentName: string | null): Promise<void> {
    const myToken = ++loadToken;
    if (!agentName) {
      all.value = [];
      query.value = "";
      cursor.value = -1;
      return;
    }
    try {
      const items = await listTriggers(agentName);
      if (myToken !== loadToken) return;
      all.value = items;
    } catch {
      // Network blip / 404 / 500 → no panel. The composer still works
      // as a plain input.
      if (myToken !== loadToken) return;
      all.value = [];
    }
    cursor.value = -1;
  }

  const results = computed<TriggerSuggestion[]>(() => {
    const q = query.value.trim();
    if (!q || q.length < minQueryLen(q)) return [];
    if (!all.value.length) return [];
    const scored: RankedSuggestion[] = [];
    for (const item of all.value) {
      const s = score(q, item);
      if (s !== Number.POSITIVE_INFINITY) {
        scored.push({ ...item, _score: s });
      }
    }
    // Stable sort: lower score first; preserve declaration order
    // within equal scores so legacy QRDic precedence (first-match-wins)
    // is reflected in the picker too.
    scored.sort((a, b) => a._score - b._score);
    return scored.slice(0, MAX_RESULTS);
  });

  const visible = computed(() => !suppressed.value && results.value.length > 0);

  // Reset the cursor whenever the result set changes — the previously
  // highlighted index might be stale (different items, fewer items,
  // or empty). ``-1`` means "no explicit highlight"; the panel still
  // treats results[0] as the default-on-Enter target.
  watch(results, () => {
    cursor.value = -1;
  });

  // When suppressed flips back on, drop the cursor too — keeps the
  // "no explicit highlight" invariant tidy across IME / blur cycles.
  watch(suppressed, (now) => {
    if (now) cursor.value = -1;
  });

  function moveCursor(delta: number): void {
    const len = results.value.length;
    if (len === 0) {
      cursor.value = -1;
      return;
    }
    if (cursor.value === -1) {
      cursor.value = delta > 0 ? 0 : len - 1;
      return;
    }
    cursor.value = (cursor.value + delta + len) % len;
  }

  const active = computed<TriggerSuggestion | null>(() => {
    if (!results.value.length) return null;
    const idx = cursor.value === -1 ? 0 : cursor.value;
    return results.value[idx] ?? null;
  });

  return {
    all,
    results,
    query,
    suppressed,
    visible,
    cursor,
    load,
    moveCursor,
    active,
  };
}

/** Pull the **last whitespace-separated token** out of the draft.
 *
 * Exported for testing. The composer calls this on every input event
 * to keep ``query`` in sync. We don't use the *full* draft because:
 *
 * - A user typing ``反馈丢失 思思`` is mid-arg; matching against the
 *   full string would mean every trigger drops out as soon as the
 *   user types the first space, which is the opposite of what we
 *   want. Last-token keeps the panel useful for long parametric
 *   triggers without a full LSP.
 * - Multi-line input (Shift+Enter) is split too — last *line*'s
 *   last token. That's how Slack / Discord behave for ``@`` and
 *   ``/`` mention completion.
 */
export function lastQueryToken(draft: string): string {
  if (!draft) return "";
  // Most recent line, last token. ``trimEnd`` so a trailing space
  // → empty query → panel hidden, which matches the "user just
  // submitted a token, waiting on next" intuition.
  const lastLine = draft.split(/\r?\n/).at(-1) ?? "";
  const trimmed = lastLine.trimEnd();
  if (trimmed !== lastLine) return ""; // trailing whitespace = no live query
  const m = lastLine.match(/(\S+)$/);
  return m ? m[1] : "";
}
