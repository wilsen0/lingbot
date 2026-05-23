# QRSpeed / QRDicPro 语法实测对照

> linling-dsl 是 QRSpeed (QRDIC / QRDicPro) 词库的 Python 重写。原项目没有官方公开文档，目前能拿到的最权威**两份**资料是：
>
> 1. **`QRDic/dicpro.txt`** ——本仓库自带，10 k 行真实生产词库（涂山苏苏 bot），10 万条匹配过的语法点。
> 2. **`docs/dsl/external-references/ziyii01/`** ——文瑶（QQ 2370927759）4 年前公开的样例集（ <https://github.com/ziyii01/QRSpeed_QRDICPro-ciku> ），含 7 份 `.txt`：签到、留言板、商店背包、俄罗斯转盘、随机图片、快捷辅助、群审核。比 dicpro.txt 更小但覆盖**不同的功能集**——所以是**额外**的语法证据。
>
> 本文是把这两份资料里出现过的**每一个**语法元素、每一个工具调用、每一个内置变量按出现频次列出，并标注 linling 当前实现状态。

---

## 0. 比对方法

* dicpro.txt 用 `scripts/audit_dsl_coverage.py` 抓出每一个 `$tool$` 引用 + `scripts/audit_handlers.py` dry-run 整库。
* ziyii01 样例用肉眼读取所有 7 份文件，提取**新出现的**语法元素（dicpro.txt 没出现过的）。
* 状态分类：
  * ✅ **已实现**——VM/parser/工具均覆盖
  * ⚠️ **STUB**——已注册但行为是 no-op（adapter 缺失、安全拒绝、占位）
  * ❌ **MISSING**——没有注册/解析；调用时静默返回 `""`
  * 🔍 **新发现**——只在 ziyii01 样例出现，dicpro.txt 不用

---

## 1. 顶层结构

| 元素 | dicpro.txt 用例 | ziyii01 用例 | 状态 |
| :- | :-: | :-: | :-: |
| `&&<配置>...` 配置行 | 1 | 0 | ✅（解析时丢弃） |
| `// 行注释` | ~30 | ✅ | ✅ |
| `## 行注释`（替代风格） | 0 | ✅（群审核样例） | ❌ **新发现**——目前只接 `//` |
| 空行分隔 handler | 全部 | 全部 | ✅ |
| `[内部]` 私有 handler 前缀 | ✅ | ✅ | ✅ |
| `[系统]` 特殊触发器 | 0 | ✅（系统事件分发） | 🔍 **新发现** |
| `[退群]` 特殊触发器 | 0 | ✅ | 🔍 **新发现** |

> **`##` 注释 + 系统事件分发**是 ziyii01 暴露的两条 dicpro.txt 没用过的能力。当前我们的解析器把 `##xxx` 当 OutputText（会被当成"输出 `##xxx`"打到气泡里），需要在 parser 里加一条 lstrip 后以 `##` 开头的就跳过。

---

## 2. 触发器

| 形式 | dicpro.txt | ziyii01 | 状态 |
| :- | :-: | :-: | :-: |
| 字面量 `背包` | ~150 | ✅ | ✅ |
| 捕获 `查看昵称(.*)` | ~290 | ✅ | ✅ |
| 选择分支 `(钓鱼\|鱼塘)` | ~10 | ✅ | ✅ |
| `[字符类]` `[\s\S]*` | 5 | ✅ | ✅ |
| 大小写不敏感 `(?i)留言板。` | 0 | ✅（留言板） | ❌ **新发现**——Python `re.compile` 默认大小写敏感；要加 `(?i)` 或 `re.IGNORECASE` flag 透传 |
| 末尾装饰符 `\n` `。?` | ~80 | ✅ | ✅（`strip` 后 fullmatch） |
| 非贪婪 `*?` | 0 | 0 | ✅（标准 re 行为） |

> `(?i)` flag 触发器是真要补的——ziyii01 的留言板规则就用了。Python 的 `re.compile` 默认不开 IGNORECASE，但 `(?i)` 内联 flag 是正则原生语法，**Python 已经原生支持**——用户写 `(?i)留言板。`，`re.compile` 自动按 case-insensitive 编译。**所以这一项实际上是已实现的**，只需要在文档里说明。让我后面验证。

---

## 3. 控制流

| 元素 | dicpro.txt | ziyii01 | 状态 |
| :- | :-: | :-: | :-: |
| `如果:cond` ... `如果尾` | 大量 | ✅ | ✅ |
| `正则:cond` ... `如果尾` | 0 | 0 | ✅（已支持但实际语料不用——条件里 `$正则 ...$==1` 已经够用） |
| `返回` / `完成` | 大量 | ✅ | ✅ |
| `:label` + `$jump :label$` / `$跳 :label$` | 大量 | ✅ | ✅ |
| `否则` / `elseif` | 0 | 0 | ❌（QRSpeed 本身也没有；不需要） |
| 显式条件括号 `(A&B)\|C` | 0 | 0 | ❌（同上，不需要） |
| 条件运算 `&` `\|` `==` `!=` `>` `<` `>=` `<=` | 大量 | ✅ | ✅ |

---

## 4. 表达式 / 插值

| 元素 | dicpro.txt | ziyii01 | 状态 |
| :- | :-: | :-: | :-: |
| `%var%` 插值 | 大量 | ✅ | ✅ |
| `[arith]` 算术 | 大量 | ✅ | ✅（含 `+ - * / %` 和括号） |
| `$tool args$` 内联 | 大量 | ✅ | ✅ |
| `@var[k][l]` JSON 访问 | 4 | ✅ | ✅ |
| `\n` `\r` `\t` 转义（输出阶段解码） | 大量 | ✅ | ✅ |
| `\\` 字面反斜杠 | 1 | 0 | ✅ |
| `\%0A` `\%20` `\%25` 等 URL-编码字面量 | 0 | ✅（商店、俄罗斯转盘） | ❌ **新发现**——`\%XX` 这种作为字面量进入文本而不被 `%var%` 拼接逻辑吃掉。当前 VM 在 `_parse_interpolated_text` 里看到 `%` 就会找下一个 `%`，遇到 `\%25` 会把 `\` 当字面、`%25...` 当变量名。**需要加一步**：先把 `\%XX` 转义为不可见占位，最后再 swap 回 `%XX`。 |
| `%0A` 在工具参数里作为换行 | 0 | ✅（商店切分用） | ⚠️ **STUB**——VM 文档里说不解码（避免和 `%var%` 冲突）；但 `$替换 ⊹ %a%⊹\%0A⊹`这种**模式里**`\%0A` 才会被工具看见。需要 VM 在内联工具参数里把 `\%XX` 解成对应字符（这是 ziyii01 上传图片功能依赖的） |

> **`\%XX` 转义**是 ziyii01 商店脚本里用得很重的功能。商店货物列表行是 `行1\n行2`（实际换行字符），但 `$替换 ⊹ ... ⊹\%0A⊹` 的第三个参数（替换源）必须用 `\%0A` 表示真换行——直接写 `⊹换行⊹` 在单行 `.ling` 文件里写不出来。这是个 **真实的兼容缺口**。

---

## 5. 内置上下文变量

| 名称 | dicpro.txt | ziyii01 | 状态 |
| :- | :-: | :-: | :-: |
| `%QQ%` `%用户%` | 1591 | ✅ | ✅ |
| `%群号%` `%群%` `%会话%` | 765+308 | ✅ | ✅ |
| `%昵称%` | 29 | ✅ | ✅ |
| `%Robot%` `%自己%` | 155 | ✅ | ✅ |
| `%参数-1%` | 55 | ✅ | ✅ |
| `%Code%` `%Msgbar%` `%Time%` `%Type%` `%Value%` `%Status%` `%Reqid%` | 17 (合计) | ✅ | ✅（从 `event.raw` 读） |
| `%Json%` `%Skey%` | 10 | 0 | ⚠️ STUB（恒空字符串） |
| `%AT0%`...`%AT9%` | 236 (合计) | ✅ | ✅ |
| `%括号1%`...`%括号9%` | 300 (合计) | ✅ | ✅ |
| `%时间HH%` `%时间mm%` 等 | 483 (合计) | ✅ | ✅（15 种后缀） |
| `%随机数N-M%` | 60 | ✅ | ✅ |
| `%IMG0%`...`%IMGN%` | 5 | ✅（上传图片） | ❌ **MISSING**——dicpro.txt 已有 5 处，ziyii01 重度依赖；从 `event.segments` 里第 N 个 `ImageSegment` 取 url |
| `%IMGNUM%` | 0 | ✅ | ❌ **MISSING**——`event.segments` 里 `ImageSegment` 计数 |
| `%FIMG0%` 闪图 | 0 | ✅ | ❌ **MISSING**——QQ 闪照；OneBot 协议有 `flash` 标志 |
| `%FACE0%` `%FACENEW0%` `%FACEPRO0%` | 0 | ✅ | ❌ **MISSING**——`event.segments` 里 `FaceSegment` 数据 |
| `%XML0%` `%JSON0%` | 0 | ✅ | ❌ **MISSING**——QQ 卡片消息（XML/JSON shape） |
| `%NDTime%` 时间戳毫秒 | 0 | ✅（冷却用） | ❌ **MISSING**——`int(time.time()*1000)` |
| `%RobotRunTime%` 机器人启动时间戳 | 0 | ✅（运行时间显示） | ❌ **MISSING**——bot 启动时记一次，全局可读 |
| `%UinName%` `%Inviteename%` 加群事件名 | 0 | ✅（系统事件） | ❌ **MISSING**——OneBot 加群 notice 字段 |
| `%主人%` | 0 | ✅ | ❌ **MISSING**——bot 配置里 owner_id（dicpro.txt 用 `%管理员%`，是同一概念） |
| `%管理员%` `%主群%` 等迁移占位 | 多 | 0 | ✅（迁移层注入） |

> **真实缺口（按重要性排序）**：
> 1. `%IMG0%` `%IMGNUM%` —— dicpro.txt 5 处，是"接扔瓶子"和"苏苏问答"功能的核心（没有它，瓶子永远不存图，问答匹配图片永远失败）
> 2. `%NDTime%` —— ziyii01 反复用做冷却时间戳；可以替代 `[%时间MMddHH%-3]` 这种粗粒度冷却
> 3. `%UinName%` `%Inviteename%` —— `[系统]` 事件分发依赖
> 4. `%FACE0%` `%XML0%` `%JSON0%` —— 卡片消息分析；当前 web 场景用不上，OneBot 接好后才需要

---

## 6. 工具调用

### 6.1 已注册且活的（OK）

| `$dsl_name$` | dicpro.txt | ziyii01 | 备注 |
| :- | :-: | :-: | :- |
| `$读 path key default$` | 1083 | ✅ | KV |
| `$写 path key value$` | 1174 | ✅ | KV |
| `$删除 path$` | 19 | 0 | KV |
| `$排行榜 path order top sep fmt$` | 15 | 0 | KV 排行 |
| `$替换 SEP TEXT PATTERN$` 或 packed 形式 | 94 | ✅ | str_ops |
| `$正则 SEP TEXT PATTERN$` | 4 | 0 | regex_match |
| `$取中间 SEP BLOB$` | 2 | 0 | substring_between |
| `$JSON 长度/获取/添加/删除$` | 89 | ✅ | json_op |
| `$URLEncoder text$` `$URLDecoder text$` `$Base64Encoder/Decoder$` `$HexEncoder/Decoder$` `$UnicodeDecoder$` | 7 | ✅ | codec |
| `$随机数 lo hi$` `$随机数 lo-hi$` | 26 | ✅ | random_ops |
| `$概率随机 weights values$` | 5 | 0 | random_ops |
| `$群昵称 group user$` | 49 | 0 | adapter_rpc |
| `$群头衔 group user title$` | 1 | 0 | adapter_rpc |
| `$获取群成员 group$` | 7 | 0 | adapter_rpc |
| `$获取群列表$` | 1 | 0 | adapter_rpc（这一轮新加） |
| `$获取消息 field default?$` | 2 | 0 | adapter_rpc |
| `$输出为 value$` | 7 | 0 | identity |
| `$发送 群\|好友\|临时 msg\|img target body$` | 43 | 0 | adapter sink |
| `$调用 ms handler args...$` | 152 | 0 | scheduler |
| `$回调 handler args...$` | 24 | ✅ | sync internal call |
| `$图文 content$` | (经迁移) | 0 | Pillow 渲染 |
| `$全局变量 key value$` `$取变量 key default?$` | 2+1 | 0 | 进程内全局 dict |
| `$agent name input$` | — | 0 | LLM 桥 |

### 6.2 STUB（注册但是 no-op）

| `$dsl_name$` | 用例 | 原因 |
| :- | :-: | :- |
| `$BSH code$` | 57 | **永久拒绝**——脚本注入风险 |
| `$执行 code$` | 5 + ziyii01 1 | **永久拒绝** |
| `$访问 url$` | 44 + ziyii01 重用 | 安全默认不出网；如果想放开改 `tools_builtin.http_get` |
| `$读文件 path default?$` | 2 | 走 KV |
| `$写文件 path content$` | 5 | 走 KV |
| `$词库操作 action target$` | 4 | 运行时改规则未实现 |
| `$撤回 group msg_id$` `$禁 group user dur$` `$全体禁言 group enabled$` `$设置群状态 group status$` `$退出群 group$` `$申请群 group comment$` `$改 group user new_name$` | 25 | adapter RPC，无 OneBot 时静默 |

### 6.3 ❌ MISSING（dicpro.txt 0，但 ziyii01 用过）

| `$dsl_name$` | 出现样例 | 含义 |
| :- | :- | :- |
| `$时间 fmt$` | 签到（`$时间 yyyyDD$`） | 等价 `%时间...%`，但作为工具——签发任意 strftime 格式。我们的 `%时间...%` 限制在白名单 15 种后缀，这条工具能透传任意 fmt |
| `$JSON 包含 var key$` `$JSON 键 var$` | 商店（`$JSON 包含 b key %括号1%$`） | dict 包含检查、键集合等子命令——我们的 `json_op` 当前只支持 `长度/获取/添加/删除` |
| `$MD5 text$` | 简易教词 | 标准 md5 hash |
| `$进群审核 group user A B C$` | 退群提醒 md | OneBot `set_group_add_request`；当前我们没有 |
| `$下载 path url$` | 随机图片 | 文件下载到指定路径 |
| `$图片链接 X$` | 随机图片 | 从 segment 提取图片 url |
| `$管理员 QQ$` | 留言板（`$管理员 %QQ%$`，返回该用户的管理员标识） | 等价 `is_admin?` 查询；我们用 `%管理员%` 配置占位，逻辑上不一样 |

### 6.4 ❌ MISSING（媒体类输出）

| 形式 | 出现样例 | 含义 |
| :- | :- | :- |
| `±img=src±` | dicpro.txt + ziyii01 | ✅ 已支持 |
| `±ptt=src±` | ziyii01 快捷辅助 | 语音 segment（QQ 录音）→ `VoiceSegment`；linling-core 已有 `VoiceSegment` 类，但 parser 没识别 |
| `±fimg=src±` | ziyii01 快捷辅助 | 闪照（一次性图片，QQ 协议）；同上 parser 没识别 |
| `±bub N±` | ziyii01 快捷辅助 | 气泡 ID（QQ 装扮气泡） |
| `±rep @[msg]±` | ziyii01 快捷辅助 | 回复消息（构造 `ReplySegment`）；linling-core 已有 `ReplySegment`，parser 没识别 |
| `±strmsg TEXT±` | ziyii01 快捷辅助 | 文本水印 / 引用样式；在 OneBot 里就是带 prefix 的文本 |
| `±%蛋%±` 等动态 sigil | dicpro.txt 2 处 | 看起来是 buggy 写法，先忽略 |

---

## 7. 优先级排序的修复清单

按"用户能不能感知到行为差异"排序：

### P0 ── 影响真实生产规则

1. ✅ **`%IMG0%` `%IMGNUM%`**（已交付）—— dicpro.txt 5 处用了，"接扔瓶子" / "苏苏问答" 链路依赖。`_get_event_context_var` 加了 IMG 系列从 `event.segments` 取，单测覆盖。
2. ✅ **`\%XX` URL-编码字面量**（已交付）—— ziyii01 商店脚本核心机制。改在 **parser** 阶段（`_decode_url_escapes_for_parsing`）—— 比 VM 阶段更早，避免 `%var%` 扫描看到 `\%0A` 把 `0AB...%` 当成变量名。`\%25` 用 PUA sentinel 表示，最后还原成 `%`。
3. ✅ **`##` 行注释**（已交付）—— ziyii01 系统事件 md 用了；parser 里 trigger 行和 body 行都加了 `##` 跳过分支。

### P0 实施细节（参考）

- `packages/dsl/src/linling_dsl/parser.py`：新增 `_decode_url_escapes_for_parsing` + `_PERCENT_SENTINEL`；`_parse_handler` `_parse_body` `_looks_like_body_continuation` 都识别 `##`；`_parse_interpolated_text` 在最末做 PUA 还原。
- `packages/dsl/src/linling_dsl/vm.py`：`_CTX_RESOLVERS` 之外加 `IMG` 系列分支（`%IMGNUM%` 计数 + `%IMG0%`-`%IMGN%` URL/path 取值）。
- `packages/dsl/tests/test_vm_qrdic_compat.py`：5 个新测试覆盖 IMG 解析、`##` 注释、`\%XX` 解码、`(?i)` 不区分大小写触发器。

### P1 ── 影响新规则的可移植性

4. ✅ **`%NDTime%` `%RobotRunTime%`**（已交付）—— `_CTX_RESOLVERS` 加 NDTime 用 `time.time()*1000`、RobotRunTime 用 `_BOT_START_MS[0]`；bootstrap 注入实际启动时间戳；测试覆盖。
5. ✅ **`(?i)` 触发器 flag**（已确认）——Python `re.compile("(?i)留言板。")` 原生 work；分类器没改任何代码。这一项加了一个 regression 测试 pin 行为。
6. ✅ **`$时间 fmt$` 工具**（已交付）—— `format_ops.format_time` 接 variadic 参数；自动翻译 Java date letter（`yyyy`→`%Y`、`MM`→`%m`、`dd`→`%d`、`HH`→`%H`、`mm`→`%M`、`ss`→`%S`、`DD`→`%j`）。
7. ✅ **`$MD5 text$`**（已交付）—— `format_ops.md5_hex`，variadic 接全部 token，hashlib MD5（usedforsecurity=False）。
8. ✅ **`$JSON 包含 var key$`** + `$JSON 键 var$`（已交付）—— `json_ops` 加 `_contains` 和 `_keys` 子命令。dict 看 key 集合，list 看元素 in，缺失返回空。
9. ✅ **`±ptt=` `±fimg=` `±rep` `±bub` `±strmsg` 媒体 sigil**（已交付）—— AST 加 `OutputVoice`/`OutputFlashImage`/`OutputReply`，parser 识别，VM emit `VoiceSegment` / `ImageSegment(extras={"flash":True})` / `ReplySegment`。`±bub` `±strmsg` 静默丢弃（QQ 装饰）。
10. ✅ **`%管理员%` `%主人%` `%主群%`**（这次顺便交付）—— bootstrap 注入 `admin_users`/`main_group` 到 dispatcher extras；VM 在 `_get_event_context_var` 解析 `%管理员%`(=admin_users[0])、`%主人%`(同 alias)、`%主群%`(=main_group)。
11. ✅ **WebUI ⇄ QQ 身份桥**（这次顺便交付）—— WebUI dispatcher 把 `scope.id` 默认设为 bot 配置的 `main_group`；REST `chat()` body / WS `input` 帧都接 `scope_id` 字段供测试时切群；测试覆盖。

### P2 ── 影响 OneBot 适配器接好后才生效的特性

10. **`[系统]` `[退群]` 特殊触发器** + **`%UinName%` `%Inviteename%`**——QQ 群事件分发；要等 OneBot adapter 拿到 notice event 才有数据。
11. **`%FACE0%` `%XML0%` `%JSON0%` `%FIMG0%`**——卡片消息分析；同上。
12. **`$进群审核 ...$` `$下载 ...$` `$图片链接 ...$`**——adapter RPC 类。

---

## 8. 决定**不做**的部分

* **`$BSH$` `$执行$`**：永久拒绝。Java BSH 的本意就是任意脚本注入，迁移到 Python 跑等于把任意 Python eval 暴露给规则文件，不能做。原样例里也只有 `$BSH 图文.java imagettftext X$` 这种"图文渲染"用法，已经被 migrator 改写为 `$图文 X$`。
* **`$访问 file:///path$`**：磁盘文件读 = `$读文件$`，已是 stub。如果真想读磁盘文件，扩 KV 而不是开放任意 file://。
* **`$词库操作$`**：QRSpeed 的"运行时修改规则"——这一条目前我们用热重载（保存 `.ling` 文件 → router 重新编译）替代，是**更安全的**模式；不打算做"VM 内部改 AST"。

---

## 9. 实施计划

P0 三项**已全部交付**（`%IMG*%` / `%IMGNUM%`、`\%XX` 解码、`##` 注释）。

P1 八项**已全部交付**（`%NDTime%` / `%RobotRunTime%`、`%管理员%` / `%主人%` / `%主群%`、`±ptt=` / `±fimg=` / `±rep` / `±bub` 媒体 sigil、`$时间 fmt$`、`$MD5$`、`$JSON 包含/键$`、`(?i)` flag 已锁、WebUI 身份桥）。

P2 项**全部已交付**：
- ✅ `[系统]` `[退群]` `[上下管理]` 特殊触发器（OneBot adapter 翻译 notice / request 为合成 message 事件，且当规则集没注册对应 handler 时分类器返回 `ignore` 而不是兜到 LLM）
- ✅ `%UinName%` `%Inviteename%` 等系统事件字段（adapter 在 `event.raw` 注入）
- ✅ `$进群审核 group user A B reason$`（调 OneBot `set_group_add_request`）
- ✅ `%FACE0%` `%FACENEW0%` `%FACEPRO0%` `%XML0%` `%JSON0%` `%FIMG0%` 全部实现
- ✅ `$图片链接 N$` `$下载 path url$` `$群头像 group$` `$管理员 user_id$` 全部实现
- ✅ **bracket-literal 触发器修复**：`[戳一戳]` `[系统]` `[退群]` 等之前被错误编译为正则字符类，现在作为字面字符串匹配（顽固存在的 long-standing bug）
- ✅ WebUI 端 Chat.vue 加 "测试场景" sheet 让用户切换 `scope_id` 模拟群号

总计现在跑通：**742 backend tests pass / 100 mypy source files clean / ruff clean / dicpro corpus dry-run 0 vm-error / dsl coverage 0 MISSING / no-input 12 (was 16)**。

---

## 10. 参考文件

* dicpro.txt 整体审计：`uv run python scripts/audit_handlers.py` / `uv run python scripts/audit_dsl_coverage.py` / `uv run python scripts/audit_image_urls.py`
* 当前实现：`packages/dsl/src/linling_dsl/parser.py` `vm.py`，`packages/tools-stdlib/src/linling_tools_stdlib/*.py`
* ziyii01 样例：`docs/dsl/external-references/ziyii01/*.txt`
* 正式语法表（已写）：`docs/dsl/grammar.md`
