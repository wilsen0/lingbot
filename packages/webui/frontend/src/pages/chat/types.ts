/**
 * Chat 模块共享类型。
 *
 * 单独抽出, 因为 ChatMessageList / ChatComposer / Chat 主页都引用 Msg, 但
 * 它不属于任何一个 .vue 文件 — 把类型沉到 ts 文件里, 避免 import .vue 仅为
 * 拿类型的尴尬。
 */

export interface MsgSegment {
  kind: "text" | "image";
  text?: string;
  url?: string;
  alt?: string;
}

export type MsgRole = "user" | "assistant" | "tool";

export interface Msg {
  id: number;
  role: MsgRole;
  content: string;
  toolName?: string;
  streaming?: boolean;
  /**
   * Rich-message segments returned by a DSL-resolved reply.
   * 当 length > 0 时, 气泡按 segments 顺序渲染; 否则用纯文 content。
   * content 本身是所有 text 段拼接, 留作 a11y / 文本选中兜底。
   */
  segments?: MsgSegment[];
}

/** done 帧里的工具调用次数 / 来源等元数据 — 现在只用 segments, 但保留扩展余地。 */
export interface AgentDoneMeta {
  source?: string;
  toolCallsMade?: number;
  totalTokens?: number;
}
