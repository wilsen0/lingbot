<template>
  <div class="chat">
    <ChatHeader
      :current-agent="currentAgent"
      :agent-count="agents.length"
      :loading-agents="loadingAgents"
      :scope="prefs.scope"
      :message-count="messages.length"
      :can-reset="!!currentAgent && (messages.length > 0 || streaming)"
      @open-drawer="$emit('open-drawer')"
      @pick-agent="onAgentClick"
      @open-scope="scopeOpen = true"
      @reset="onResetClick"
    />

    <ChatMessageList
      ref="msgListRef"
      :messages="messages"
      :streaming="streaming"
      :new-msg-token="newMsgToken"
    >
      <template #empty>
        <div v-if="!messages.length && !streaming" class="chat__empty">
          <p class="chat__empty-poem font-display">
            <template v-if="currentAgent">开始聊天</template>
            <template v-else-if="loadingAgents">正在加载助手</template>
            <template v-else>尚未接入助手</template>
          </p>
          <p v-if="!currentAgent && !loadingAgents" class="chat__empty-hint">
            当前还没有可用助手。接入助手后即可开始对话。
          </p>
          <p v-else-if="currentAgent" class="chat__empty-hint">
            <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行 · <kbd>Esc</kbd> 关闭候选
          </p>
        </div>
      </template>
    </ChatMessageList>

    <ChatComposer
      ref="composerRef"
      :disabled="!currentAgent"
      :streaming="streaming"
      :placeholder="currentAgent ? '输入消息' : '尚未接入助手'"
      :agent-name="currentAgentName"
      @submit="onComposerSubmit"
      @cancel="cancel"
    />

    <ChatAgentPicker
      v-model:open="agentPickerOpen"
      :agents="agents"
      :current-name="currentAgentName"
      @pick="onPickAgent"
    />

    <ChatScopeSheet v-model:open="scopeOpen" :current="prefs.scope" @apply="onScopeApply" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { listAgents, type AgentSummary } from "@/api/agents";
import { confirmDestructive, confirmReroute } from "@/composables/useConfirm";
import { toast } from "@/composables/useToast";
import { usePrefsStore } from "@/store/prefs";

import ChatAgentPicker from "./chat/ChatAgentPicker.vue";
import ChatComposer from "./chat/ChatComposer.vue";
import ChatHeader from "./chat/ChatHeader.vue";
import ChatMessageList from "./chat/ChatMessageList.vue";
import ChatScopeSheet from "./chat/ChatScopeSheet.vue";
import { useConversation } from "./chat/useConversation";

defineEmits<(e: "open-drawer") => void>();

/**
 * Chat 页 — 编排层。
 *
 * 所有"实质工作"已下沉到子组件 / composable:
 *   useConversation : WS 生命周期 / 消息状态 / 流式 delta 合并 / 完成态副作用
 *   ChatHeader      : 顶 bar + 4 个动作按钮
 *   ChatMessageList : 消息渲染 + 滚动跟手 + 粘底
 *   ChatComposer    : 输入条 + IME guard + autosize + 发布 dock 高度
 *   ChatAgentPicker / ChatScopeSheet : 两个轻量浮签
 *
 * 此处只做"编排":
 *   • 页面级初始化 (拉 agent 列表, 选第一个)
 *   • 用户主动语义 (二次确认 / 切换 / 焚此缘)
 *   • 全局快捷键 (Cmd+K / /)
 *   • 把 prefs.scope 喂给 conversation.send()
 */

const prefs = usePrefsStore();
// 解构 — 让模板可以直接写 messages / streaming, 自动解 ref;
// 否则 conv.messages 在模板里是 Ref 本身, 要 conv.messages.value, 重复且易错。
const { messages, streaming, newMsgToken, openFor, send, cancel, reset } = useConversation();

const agents = ref<AgentSummary[]>([]);
const loadingAgents = ref(true);
const currentAgentName = ref<string | null>(null);
const currentAgent = computed(
  () => agents.value.find((a) => a.name === currentAgentName.value) ?? null,
);

const agentPickerOpen = ref(false);
const scopeOpen = ref(false);

const msgListRef = ref<InstanceType<typeof ChatMessageList> | null>(null);
const composerRef = ref<InstanceType<typeof ChatComposer> | null>(null);

function onAgentClick() {
  if (!agents.value.length) return;
  if (agents.value.length === 1) {
    toast.info("当前只有一个助手", agents.value[0].name);
    return;
  }
  agentPickerOpen.value = true;
}

async function onResetClick() {
  if (!currentAgent.value) return;
  if (!messages.value.length && !streaming.value) return;
  const ok = await confirmDestructive("清空对话", "清空后这段内容将无法恢复。", "清空");
  if (!ok) return;
  reset();
  composerRef.value?.clear();
  msgListRef.value?.scrollToBottom();
}

async function onPickAgent(name: string) {
  if (name === currentAgentName.value) {
    agentPickerOpen.value = false;
    return;
  }
  if (messages.value.length > 0) {
    const ok = await confirmReroute("切换助手", "当前对话会先清空，再切到新的助手。", "切换");
    if (!ok) return;
  }
  currentAgentName.value = name;
  agentPickerOpen.value = false;
  reset();
  composerRef.value?.clear();
  openFor(name);
  toast.info("已切换助手", name);
}

function onScopeApply(value: string) {
  prefs.setScope(value);
  scopeOpen.value = false;
  toast.info(value ? "已切换测试场景" : "已恢复默认", value || "你的账号");
}

async function onComposerSubmit(text: string) {
  await send(text, prefs.scope || undefined);
  msgListRef.value?.scrollToBottom();
}

/* === 全局快捷键: / 聚焦输入, Cmd/Ctrl+K 打开切换, Esc 关 sheet === */
function onShortcut(ev: KeyboardEvent) {
  if (ev.isComposing) return; // composing 时一切快捷键让位 IME

  const target = ev.target as HTMLElement | null;
  const typing =
    target &&
    (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);

  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
    ev.preventDefault();
    if (agents.value.length > 1) agentPickerOpen.value = true;
    return;
  }
  if (!typing && ev.key === "/") {
    ev.preventDefault();
    composerRef.value?.focus();
  }
}

onMounted(async () => {
  // Listener 必须在 await 之前注册 — 否则用户在 await listAgents() 期间快速
  // 离开页面时, onBeforeUnmount 会先于 listener 注册触发, 后者就泄漏了
  window.addEventListener("keydown", onShortcut);

  try {
    const list = await listAgents();
    agents.value = list;
    if (list.length && !currentAgentName.value) {
      currentAgentName.value = list[0].name;
      openFor(list[0].name);
    }
  } catch {
    /* noop — 没拉到也保留空态 */
  } finally {
    loadingAgents.value = false;
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onShortcut);
});
</script>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  /*
   * height: 100% 而不是 min-height — shell-main 现在是固定高度 flex
   * item (overflow:hidden), Chat 必须正好填满, 不能溢出 (溢出会让
   * shell-main 又能滚, 失去"锁住外层滚动"的意义).
   */
  height: 100%;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

/* 空态 — 只有"在中央居中放一段诗"这一件事 */
.chat__empty {
  margin: auto;
  padding: clamp(10vh, 22vh, 30vh) 0 clamp(6vh, 14vh, 20vh);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--gap-md);
  animation: var(--motion-fade-in-up);
  text-align: center;
}
.chat__empty::before {
  content: "";
  width: 72px;
  height: 1px;
  margin-bottom: 2px;
  background: linear-gradient(to right, transparent, rgb(var(--color-thread) / 0.5), transparent);
  filter: drop-shadow(0 1px 2px rgb(var(--color-thread) / 0.25));
}
.chat__empty-poem {
  font-size: clamp(15px, 4vw, 18px);
  letter-spacing: 0.22em;
  color: rgb(var(--color-ink) / 0.9);
  /* 浅色主题用浅墨 + 朱影; 深色主题保留黑色阴影 */
  text-shadow: 0 1px 2px rgb(var(--color-sorrow) / 0.15);
  padding: 0 12px;
}
:root[data-theme="dark"] .chat__empty-poem,
:root[data-theme="auto"] .chat__empty-poem {
  text-shadow: 0 2px 6px rgb(0 0 0 / 0.5);
}
.chat__empty-hint {
  font-size: 12px;
  letter-spacing: var(--track-meta);
  color: rgb(var(--color-ink) / 0.88);
  text-align: center;
  max-width: 28em;
  line-height: 1.7;
}
.chat__empty-hint code {
  background: rgb(var(--color-ink) / 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: rgb(var(--color-ink));
  letter-spacing: 0;
}
.chat__empty-hint kbd {
  display: inline-block;
  padding: 1px 6px;
  background: rgb(var(--color-ink) / 0.08);
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: rgb(var(--color-ink));
  letter-spacing: 0;
}
</style>
