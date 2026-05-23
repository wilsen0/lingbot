import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";

import type { TriggerSuggestion } from "@/api/agents";
import ChatComposer from "@/pages/chat/ChatComposer.vue";
import { lastQueryToken, useTriggerSuggest } from "@/pages/chat/useTriggerSuggest";

vi.mock("@/api/agents", async () => {
  const actual = await vi.importActual<typeof import("@/api/agents")>("@/api/agents");
  return {
    ...actual,
    listTriggers: vi.fn(),
  };
});

import { listTriggers } from "@/api/agents";

function s(label: string, raw?: string, has_args = false): TriggerSuggestion {
  return {
    raw: raw ?? label,
    label,
    has_args,
    literal_prefix: has_args ? label.split("…")[0] : label,
  };
}

/** Mount a tiny host so the composable's reactive lifecycle behaves
 * as it would inside ``ChatComposer.vue``. The host renders nothing —
 * we only care about the returned controller. */
function mountController() {
  let captured!: ReturnType<typeof useTriggerSuggest>;
  const Host = defineComponent({
    setup() {
      captured = useTriggerSuggest();
      return () => h("div");
    },
  });
  const wrapper = mount(Host);
  return { wrapper, ctrl: captured };
}

function mountComposer() {
  return mount(ChatComposer, {
    props: {
      disabled: false,
      streaming: false,
      placeholder: "言於此",
      agentName: "susu",
    },
    attachTo: document.body,
  });
}

describe("lastQueryToken", () => {
  it("returns empty for empty draft", () => {
    expect(lastQueryToken("")).toBe("");
  });

  it("returns last token of single line", () => {
    expect(lastQueryToken("我的灵玉")).toBe("我的灵玉");
    expect(lastQueryToken("反馈丢失 思思")).toBe("思思");
  });

  it("returns empty when draft ends with whitespace", () => {
    // Trailing space: user just sent a token, panel should hide
    // (otherwise we'd keep showing the previous token's matches).
    expect(lastQueryToken("反馈 ")).toBe("");
    expect(lastQueryToken("反馈\n")).toBe("");
  });

  it("works across multiple lines (last line, last token)", () => {
    expect(lastQueryToken("hi\n反馈丢失")).toBe("反馈丢失");
    expect(lastQueryToken("hi\n反馈 思思")).toBe("思思");
  });
});

describe("useTriggerSuggest ranking", () => {
  beforeEach(() => {
    vi.mocked(listTriggers).mockResolvedValue([
      s("我的灵玉"),
      s("反馈丢失…", "反馈丢失(.*)", true),
      s("反馈吞玉…", "反馈吞玉([0-9]+)", true),
      s("查看消息"),
      s("好运赠送…", "好运赠送(.*)", true),
    ]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("hides panel for empty query", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "";
    expect(ctrl.results.value).toEqual([]);
    expect(ctrl.visible.value).toBe(false);
  });

  it("filters by prefix (CJK 1-char minimum)", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "反";
    await nextTick();
    expect(ctrl.results.value.map((r) => r.label)).toEqual([
      "反馈丢失…",
      "反馈吞玉…",
    ]);
  });

  it("requires 2 chars for ASCII queries", async () => {
    const { ctrl } = mountController();
    vi.mocked(listTriggers).mockResolvedValue([s("ping")]);
    await ctrl.load("susu");
    ctrl.query.value = "p";
    await nextTick();
    expect(ctrl.results.value).toEqual([]);
    ctrl.query.value = "pi";
    await nextTick();
    expect(ctrl.results.value.map((r) => r.label)).toEqual(["ping"]);
  });

  it("ranks exact prefix above substring", async () => {
    const { ctrl } = mountController();
    vi.mocked(listTriggers).mockResolvedValue([
      s("丢失反馈"),
      s("反馈丢失…", "反馈丢失(.*)", true),
    ]);
    await ctrl.load("susu");
    ctrl.query.value = "反馈";
    await nextTick();
    expect(ctrl.results.value.map((r) => r.label)).toEqual([
      "反馈丢失…",
      "丢失反馈",
    ]);
  });

  it("suppress flag hides panel even with matches", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "反";
    await nextTick();
    expect(ctrl.visible.value).toBe(true);
    ctrl.suppressed.value = true;
    expect(ctrl.visible.value).toBe(false);
  });

  it("loading null clears state", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "我";
    await nextTick();
    expect(ctrl.results.value.length).toBeGreaterThan(0);
    await ctrl.load(null);
    await nextTick();
    expect(ctrl.results.value).toEqual([]);
  });

  it("late response from previous agent is dropped", async () => {
    // Slow A → fast B → A's late response must not poison B's panel.
    const { ctrl } = mountController();
    let resolveA!: (v: TriggerSuggestion[]) => void;
    vi.mocked(listTriggers).mockImplementationOnce(
      () => new Promise<TriggerSuggestion[]>((res) => (resolveA = res)),
    );
    vi.mocked(listTriggers).mockResolvedValueOnce([s("BBB")]);

    const aPromise = ctrl.load("agentA");
    const bPromise = ctrl.load("agentB");
    await bPromise;
    expect(ctrl.all.value.map((t) => t.label)).toEqual(["BBB"]);
    // Now resolve A's slow response — it should be ignored.
    resolveA([s("AAA")]);
    await aPromise;
    await flushPromises();
    expect(ctrl.all.value.map((t) => t.label)).toEqual(["BBB"]);
  });

  it("network error degrades to empty silently", async () => {
    const { ctrl } = mountController();
    vi.mocked(listTriggers).mockRejectedValueOnce(new Error("offline"));
    await ctrl.load("susu");
    expect(ctrl.all.value).toEqual([]);
  });

  it("cursor wraps around with moveCursor", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "反";
    await nextTick();
    expect(ctrl.cursor.value).toBe(-1);
    ctrl.moveCursor(1);
    expect(ctrl.cursor.value).toBe(0);
    ctrl.moveCursor(1);
    expect(ctrl.cursor.value).toBe(1);
    ctrl.moveCursor(1);
    expect(ctrl.cursor.value).toBe(0); // wrap
    ctrl.moveCursor(-1);
    expect(ctrl.cursor.value).toBe(1); // wrap back
  });

  it("active is results[0] when cursor=-1, results[cursor] otherwise", async () => {
    const { ctrl } = mountController();
    await ctrl.load("susu");
    ctrl.query.value = "反";
    await nextTick();
    expect(ctrl.active.value?.label).toBe("反馈丢失…");
    ctrl.moveCursor(1); // → 0
    ctrl.moveCursor(1); // → 1
    expect(ctrl.active.value?.label).toBe("反馈吞玉…");
  });

  it("dedupes by label is the server's job; local handles it gracefully", async () => {
    // The server already dedupes; this just asserts that if duplicates
    // *did* leak through, ranking still produces a stable list.
    const { ctrl } = mountController();
    vi.mocked(listTriggers).mockResolvedValue([
      s("a"),
      s("a", "a-2"),
      s("ab"),
    ]);
    await ctrl.load("susu");
    ctrl.query.value = "ab";
    await nextTick();
    expect(ctrl.results.value.map((r) => r.label)).toContain("ab");
  });
});

describe("ChatComposer suggest flow", () => {
  beforeEach(() => {
    vi.mocked(listTriggers).mockResolvedValue([
      s("我的灵玉"),
      s("反馈丢失…", "反馈丢失(.*)", true),
      s("反馈吞玉…", "反馈吞玉([0-9]+)", true),
    ]);
  });

  it("Esc hides suggestions but fresh input brings them back", async () => {
    const wrapper = mountComposer();
    await flushPromises();
    const input = wrapper.get("textarea");

    await input.setValue("反");
    await nextTick();
    expect(wrapper.find(".suggest").exists()).toBe(true);
    expect(wrapper.findAll(".suggest__item.is-active")).toHaveLength(0);

    await input.trigger("keydown", { key: "Escape" });
    await nextTick();
    expect(wrapper.find(".suggest").exists()).toBe(false);

    await input.setValue("反馈");
    await nextTick();
    expect(wrapper.find(".suggest").exists()).toBe(true);

    wrapper.unmount();
  });

  it("submit and clear both drop stale query state", async () => {
    const wrapper = mountComposer();
    await flushPromises();
    const input = wrapper.get("textarea");

    await input.setValue("反");
    await nextTick();
    expect(wrapper.find(".suggest").exists()).toBe(true);

    await input.trigger("keydown", { key: "Enter" });
    await nextTick();
    expect((input.element as HTMLTextAreaElement).value).toBe("");
    expect(wrapper.find(".suggest").exists()).toBe(false);

    await input.setValue("反");
    await nextTick();
    expect(wrapper.find(".suggest").exists()).toBe(true);

    await (wrapper.vm as { clear: () => void }).clear();
    await nextTick();
    expect((input.element as HTMLTextAreaElement).value).toBe("");
    expect(wrapper.find(".suggest").exists()).toBe(false);

    wrapper.unmount();
  });
});
