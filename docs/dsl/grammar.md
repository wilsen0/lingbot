# Linling DSL 语法参考（含实现状态对照）

> 作用域：本文档**完整列出 QRSpeed / QRDicPro DSL 的所有语法元素**，对照 linling-dsl 的实现状态，以方便追溯进度。
>
> 信息来源：
> 1. `QRDic/dicpro.txt`（涂山苏苏生产词库，10 k 行）—— 真实的功能集证据。
> 2. `docs/dsl/external-references/ziyii01/`（QRSpeed 公开样例集）—— 不同维度的语法证据。
> 3. linling 的实现：`packages/dsl/src/linling_dsl/{parser,ast_nodes,vm,dispatcher,linter}.py`，工具实现：`packages/core/src/linling_core/tools_builtin.py` + `packages/tools-stdlib/src/linling_tools_stdlib/*.py`。
>
> 状态图例：
> - ✅ 已实现并通过测试
> - ⚠️ STUB（注册但是 no-op，等待 adapter / 安全拒绝 / 占位）
> - ❌ 未实现
> - 🔍 设计上不打算做（明确放弃）

---

## 0. 文件结构

一个 `.ling` 文件 = 多个 handler，handler 之间用空行分隔。

```
&&<配置>兼容模式:是      # 全局配置行（被解析器忽略，仅供编辑器/迁移器读）

我的灵玉                  # 第 1 个处理器：触发器 + 处理体
玉:$读 小苏苏/灵玉 %QQ% 0$
%玉%
±img=https://xxx.png±

[内部]内部目标            # 第 2 个处理器：[内部] 标记
处理体...
```

| 元素 | 状态 | 说明 |
| :- | :-: | :- |
| 触发器（首行） + 处理体 | ✅ | 用空行分隔；单空行后跟续体也接受。 |
| `[内部]` 私有 handler | ✅ | 不被分类器命中；只能 `$jump :label$` / `$回调$` / `$调用$` 进入。 |
| `&&前缀行` | ✅（忽略） | 全局配置；解析器丢弃。 |
| `// 行注释` | ✅ | 整行注释；处理器顶部 `//触发器` 整段被丢弃。 |
| `## 行注释` | ✅ | 与 `//` 等价（兼容 ziyii01 样例风格）。 |
| `[系统]` 特殊触发器 | ✅（OneBot adapter） | OneBot 加群 / 加群申请 / 好友申请等事件被翻译成合成 message 事件 + `[系统]` 文本 + `event.raw` 填 `Status`/`Code`/`Reqid` 等。 |
| `[退群]` 特殊触发器 | ✅（OneBot adapter） | 群成员退出事件 → 合成 `[退群]` message 事件。 |
| `[上下管理]` 特殊触发器 | ✅（OneBot adapter） | 群管理员变更事件 → 合成 `[上下管理]` message 事件 + `Value` 字段（1=升 0=降）。 |

---

## 1. 触发器（trigger）

| 形式 | 例子 | 状态 |
| :- | :- | :-: |
| 字面量 | `我的灵玉` `[戳一戳]` | ✅ |
| 捕获组 | `查看昵称(.*)` `补偿([0-9]+)数量([0-9]+)` | ✅ |
| 选择分支 | `(查看消息\|消息)` | ✅ |
| 字符类 | `(.*)国庆(.*)` | ✅ |
| 装饰末尾 `\n` | `XXX\n` | ✅（解析器 `strip()` 后 `fullmatch`） |
| 大小写不敏感 `(?i)` flag | `(?i)留言板help` | ✅（Python `re.compile` 原生） |
| 命令前缀 `/` `!` | 配置项 `command_prefixes`，缺省 `("/", "!")` | ✅ |
| 隐式触发器 | 无前缀直接打 `我的灵玉` | ✅ |
| 命令前缀但无匹配 | 触发 `Intent(kind="command", match=None)` 走 `unknown_command_reply` 而不是 LLM | ✅ |

---

## 2. 控制流

| 元素 | 状态 | 说明 |
| :- | :-: | :- |
| `如果:cond` ... `如果尾` | ✅ | |
| `正则:cond` ... `如果尾` | ✅ | 解析路径与 `如果:` 等价；语义靠条件文本里 `$正则 ...$==1` 实现 |
| `返回` / `完成` | ✅ | 立即终止 handler |
| `:label` 定义 | ✅ | |
| `$jump :label$` | ✅ | |
| `$跳 :label$` | ✅ | 中文别名 |
| 条件运算 `==` `!=` `>` `<` `>=` `<=` | ✅ | 数值优先，字符串回退 |
| 条件连接 `&` `\|` | ✅ | 短路；`\|` 优先级低于 `&`（解析器先按 `\|` 切） |
| 否则 / `else` / `elseif` | 🔍 不做 | QRSpeed 本身就没有；用并列 `如果:` 实现 |
| 显式条件括号 `(A&B)\|C` | 🔍 不做 | 同上 |
| 跨 handler 跳转 | 🔍 不做 | 用 `$回调$` / `$调用$` 跨 handler |

---

## 3. 表达式 / 插值

| 元素 | 例子 | 状态 |
| :- | :- | :-: |
| `%var%` 插值 | `你好 %昵称%` | ✅ |
| `[arith]` 算术 | `还差 [%上限%-%玉%] 灵玉` | ✅（`+ - * / %` + 括号 + 浮点） |
| 内联工具调用 | `%QQ% 拥有 $读 小苏苏/灵玉 %QQ% 0$ 灵玉` | ✅ |
| `@var[k][l]` JSON 访问 | `@profile[stats][hp]` | ✅ |
| `\n` `\r` `\t` 转义（输出时解码） | | ✅ |
| `\\` 字面反斜杠 | | ✅ |
| `\%XX` URL-编码字面量 | `\%0A`→换行 `\%20`→空格 `\%25`→`%` | ✅（parser 阶段解码，不和 `%var%` 互相干扰） |
| 行内函数嵌套 | `$替换 @ %x% $读 ...$$` | ✅ |
| 字面量 `{...}` JSON 对象 | `c:{}` `b:{"key":"val"}` | ✅（视为 Literal，被工具按需解析） |

---

## 4. 内置上下文变量

### 4.1 单名变量

| 变量 | dicpro.txt 用例 | ziyii01 用例 | 状态 | 说明 |
| :- | :-: | :-: | :-: | :- |
| `%QQ%` `%用户%` | 1591 | ✅ | ✅ | `event.sender.id` |
| `%群号%` `%群%` `%会话%` | 765+308+0 | ✅ | ✅ | `event.scope.id` |
| `%昵称%` | 29 | ✅ | ✅ | `display_name` 否则 `id` |
| `%Robot%` `%自己%` | 155 | ✅ | ✅ | bot id |
| `%参数-1%` | 55 | ✅ | ✅ | 整条原文 |
| `%Code%` | 1 | 0 | ✅ | `event.raw["operator_id"]` |
| `%Msgbar%` | 9 | ✅ | ✅ | `event.raw["message_id"]` |
| `%Time%` | 1 | 0 | ✅ | `event.raw["time"]` |
| `%Type%` | 1 | 0 | ✅ | `event.raw["sub_type"]` |
| `%Value%` | 2 | 0 | ✅ | `event.raw["value"]` |
| `%Status%` | 1 | ✅ | ✅ | `event.raw["status"]` |
| `%Reqid%` | 4 | 0 | ✅ | `event.raw["request_id"]` |
| `%UinName%` | 0 | ✅ | ✅ | OneBot notice 字段（加群事件被影响用户的昵称） |
| `%Inviteename%` | 0 | ✅ | ✅ | OneBot notice 字段（操作者 / 邀请人昵称） |
| `%Json%` `%Skey%` | 5+5 | 0 | ⚠️ STUB | QRDic adapter 私有字段，恒空 |
| `%管理员%` `%主人%` | （migrator 注入） | ✅ | ✅ | bot 配置 `admin_users[0]` |
| `%NDTime%` | 0 | ✅ | ✅ | 当前毫秒时间戳 |
| `%RobotRunTime%` | 0 | ✅ | ✅ | bot 启动毫秒时间戳（`set_bot_start_time_ms`） |

### 4.2 模式变量

| 模式 | 含义 | 状态 |
| :- | :- | :-: |
| `%AT0%` … `%AT9%` | 第 N 个 `AtSegment.user_id` | ✅ |
| `%括号1%` … `%括号9%` | 触发器正则的第 N 个 capture（1-based） | ✅ |
| `%时间HH%` `%时间mm%` `%时间MMddHH%` 等 | 见下表，15 种白名单 | ✅ |
| `%随机数N-M%` | `random.randint(N, M)` | ✅ |
| `%IMG0%` … `%IMGN%` | 第 N 个 `ImageSegment.url`（或 `path` / `b64`） | ✅ |
| `%IMGNUM%` | `ImageSegment` 数量 | ✅ |
| `%FIMG0%` … 闪图 | 索引带 `extras.flash=True` 的 `ImageSegment`；`%FIMGNUM%` 计数 | ✅ |
| `%FACE0%` `%FACENEW0%` `%FACEPRO0%` | 索引 `FaceSegment.face_id`（三种 alias 都映射同一来源） | ✅ |
| `%XML0%` | 索引 `XmlSegment.xml` | ✅ |
| `%JSON0%` | 索引 `CardSegment.payload`（与 `%Json%` 不同名空间） | ✅ |

#### 4.2.1 `%时间...%` 后缀（15 种）

| 后缀 | strftime | 用例 |
| :- | :- | :-: |
| 空 | `%H:%M:%S` | ✅ |
| `HH` | `%H` | ✅ |
| `mm` | `%M` | ✅ |
| `dd` | `%d` | ✅ |
| `HHmm` | `%H%M` | ✅ |
| `MMdd` | `%m%d` | ✅ |
| `MMddHH` | `%m%d%H` | ✅ |
| `yyyyMM` | `%Y%m` | ✅ |
| `yyyyMMdd` | `%Y%m%d` | ✅ |
| `yyMMdd` | `%y%m%d` | ✅ |
| `ddHH` | `%d%H` | ✅ |
| `HH:mm` | `%H:%M` | ✅ |
| `hh:mm` | `%I:%M` | ✅ |
| `hh:mm:dd` | `%I:%M:%S` | ✅ |
| `dd日HH:mm` | `%d日%H:%M` | ✅ |

任意其他 fmt 走 `$时间 fmt$` 工具（见 §5）。

---

## 5. 内置工具（DSL `$name$` 调用）

### 5.1 已注册并实现（OK）

| `$dsl_name$` | 函数 | dicpro.txt | ziyii01 | 备注 |
| :- | :- | :-: | :-: | :- |
| `$读 path key default$` | `dsl_read_kv` | 1083 | ✅ | KV 读 |
| `$写 path key value$` | `dsl_write_kv` | 1174 | ✅ | KV 写 |
| `$删除 path$` | `dsl_delete_kv` | 19 | 0 | KV 删 |
| `$排行榜 path order top sep fmt$` | `dsl_rank_kv` | 15 | 0 | 排行 |
| `$替换 SEP TEXT PATTERN$` 或 packed | `replace_sep` | 94 | ✅ | 两种 calling 形式都接受 |
| `$正则 SEP TEXT PATTERN$` | `regex_match` | 4 | 0 | 命中返回 `1` 否则 `0` |
| `$取中间 SEP BLOB$` | `substring_between` | 2 | 0 | |
| `$JSON 长度 var$` | `json_op` 子命令 | 89（合计） | ✅ | |
| `$JSON 获取 var path$` | 同上 | | ✅ | |
| `$JSON 添加 var value$` | 同上 | | ✅ | 数组追加（非数组自动初始化） |
| `$JSON 删除 var idx$` | 同上 | | ✅ | |
| `$JSON 包含 var key$` | 同上 | 0 | ✅（商店） | dict 看 key，list 看元素，**新加** |
| `$JSON 键 var$` | 同上 | 0 | 0 | 返回键集合的 JSON 数组，**新加** |
| `$URLEncoder text$` | `url_encode` | 3 | ✅ | |
| `$URLDecoder text$` | `url_decode` | 1 | 0 | |
| `$Base64Encoder/Decoder text$` | codec | 0+1 | 0 | |
| `$HexEncoder/Decoder text$` | codec | 1+1 | 0 | |
| `$UnicodeDecoder text$` | codec | 1 | 0 | 解 `\uXXXX` |
| `$随机数 lo hi$` 或 `lo-hi` | `random_int` | 26 | ✅ | |
| `$概率随机 weights values$` | `weighted_random` | 5 | 0 | 加权抽样 |
| `$群昵称 group user$` | `group_nickname` | 49 | 0 | 缺 adapter 时 fallback user_id |
| `$获取群成员 group$` | `group_members` | 7 | 0 | 缺 adapter 时返回 `[]` |
| `$获取群列表$` | `group_list` | 1 | 0 | 缺 adapter 时返回 `[]` |
| `$进群审核 group user A B reason$` | `group_add_request` | 0（dicpro.txt 没用） | ✅（ziyii01 群审核 md） | 调 OneBot `set_group_add_request`，flag 从 `event.raw["flag"]` 取（合成 [系统] 事件已经填好） |
| `$图片链接 N$` | `image_link` | 0 | ✅（ziyii01 随机图片） | 等价 `%IMGN%` 的工具形式 |
| `$下载 path url$` | `download_file` | 0 | ✅（ziyii01 随机图片） | 沙盒化文件下载，需 `data_root` 配置 |
| `$群头像 group_id$` | `group_avatar` | 1 | 0 | QQ 头像 CDN url |
| `$管理员 user_id$` | `is_admin` | 0 | ✅（ziyii01 留言板） | 是否管理员检查；返回 `1` 或 `""` |
| `$获取消息 field default?$` | `get_message_field` | 2 | 0 | 读 `event.raw[field]` |
| `$输出为 value$` | `emit_var` | 7 | 0 | 恒等返回 |
| `$发送 群\|好友\|临时 msg\|img target body$` | `send_message` | 43 | 0 | 经 adapter sink |
| `$调用 ms handler args...$` | `schedule_handler` | 152 | 0 | 经 Scheduler 延时触发 |
| `$回调 handler args...$` | `callback_stub` | 24 | ✅ | 同步内部调用 |
| `$图文 content$` | `image_text` | (经迁移) | 0 | Pillow 渲染 |
| `$全局变量 key value$` `$取变量 key default?$` | `set_global` / `get_global` | 2+1 | 0 | 进程内 dict |
| `$时间 fmt$` | `format_time` | 0 | ✅（签到） | 任意 strftime fmt（含 Java date letter 翻译），**新加** |
| `$MD5 text$` | `md5_hex` | 0 | ✅（简易教词、留言板） | 32 hex digest，**新加** |
| `$agent name input$` | `agent_invoke` | — | 0 | LLM 桥 |

### 5.2 STUB（注册但是 no-op）

| `$dsl_name$` | 用例 | 原因 / 何时生效 |
| :- | :-: | :- |
| `$BSH code$` | 57 | **永久拒绝** —— 脚本注入风险（图文渲染走 `$图文$`） |
| `$执行 code$` | 5 + ziyii01 | **永久拒绝** —— 同上 |
| `$访问 url$` | 44 + ziyii01 | 安全默认不出网；要放开改 `tools_builtin.http_get` |
| `$读文件 path default?$` | 2 | 走 KV |
| `$写文件 path content$` | 5 | 走 KV |
| `$词库操作 action target$` | 4 | 用热重载替代 |
| `$撤回 group msg_id$` 等群管 | 25 | 缺 adapter 时静默；OneBot 接好后生效 |

### 5.3 ❌ 未实现（罕用，保留待办）

| `$dsl_name$` | 来源 | 说明 |
| :- | :- | :- |
| （无） | | 全部 dicpro.txt + ziyii01 引用的工具均已注册（OK 或 STUB） |

---

## 6. 输出语句

| 形式 | 说明 | 状态 |
| :- | :- | :-: |
| 文本（任意非控制行） | `OutputText`，含 `%var%` 插值 | ✅ |
| `±img=src±` | `OutputImage` → `ImageSegment` | ✅ |
| `±img=@pic:X±` | WebUI dispatcher 改写为 `/api/files/assets/picture/X.jpg`（无后缀默认补 `.jpg`），OneBot 适配器改写为 `file:///<base_dir>/assets/picture/X.jpg`；资产存放于 `bot/assets/picture/` | ✅ |
| 远程 `±img=https://...±` | WebUI dispatcher 改写为 `/api/files/proxy?url=...` 走同源代理（CSP 安全） | ✅ |
| `±ptt=URL±` | `OutputVoice` → `VoiceSegment`，**新加** | ✅ |
| `±fimg=URL±` | `OutputFlashImage` → `ImageSegment(extras={"flash":True})`，**新加** | ✅ |
| `±rep msg_id±` | `OutputReply` → `ReplySegment`，**新加** | ✅ |
| `±bub N±` `±strmsg X±` 等 QQ 装饰 | parser 接受语法但运行时丢弃，**新加** | ✅（safe drop） |
| 多行文本 | dispatcher `_segments_to_action` 把连续 TextSegment 合并为一个气泡的多行 | ✅ |

输出限制：单 handler 最多 20 段；最多 10000 步；超 2 秒抛 `SandboxError`；都可由 dispatcher 构造时覆盖。

---

## 7. 赋值

QRSpeed 最容易踩坑的语法：

```
玉:$读 小苏苏/灵玉 %QQ% 0$    # 赋值
tip:只有双方互相申请才能互删   # 不是赋值！是 OutputText
```

判别规则（`parser._try_parse_assign`）：

1. **名字形状**：`:` 前的部分必须 ≤ 2 字符 + 不含空格 + 不以 `$` `%` `[` `±` 开头 + 不是 `如果` / `正则`。
2. **值形状**：`:` 后的部分必须是
    - `$tool ...$` / `[arith]` / `%var%` / `@json` / `{...}` / 空值（清空），**或**
    - 不含空白且不含中文标点（`，。！？；：、（）【】「」《》…—`）的"单 token"。

两个条件都满足才视为 `Assign`，否则当 `OutputText`。在 `dicpro.txt` 整库 3000+ 个真实赋值上 0 误判。

---

## 8. 多平台身份桥（QQ ⇄ WebUI）

部署约定：**WebUI 账号 = QQ 号**。用户使用相同账号（QQ 号）在两端登录，确保规则状态在 KV 中互相对齐。

| 上下文变量 | QQ 端来源 | WebUI 端来源 | 状态 |
| :- | :- | :- | :-: |
| `%QQ%` | `event.sender.id`（QQ 号） | `event.sender.id` = WebUI 用户名 = QQ 号 | ✅ 一致 |
| `%群号%` | `event.scope.id`（QQ 群号） | 默认 = 合成 DM scope（``%群号%==0``），可被请求 / WS 帧的 `scope_id` 覆盖 | ✅ 一致 |
| `%昵称%` | QQ 群昵称或 QQ 昵称 | WebUI 用户名（暂时） | ⚠️ 需要 OneBot adapter 才能拿到真群昵称 |
| `%AT0%` … | QQ AT 段 | 暂无（WebUI 不支持 AT 段） | ⚠️ |
| `%IMG0%` … | QQ 图片消息 | WebUI 上传图片（后续） | ⚠️ |

**关键设计决定（WebUI scope = 合成 DM）**：

QRSpeed 约定 `%群号%==0` 为私聊。`dicpro.txt` 里 group-only 规则一般以 `如果:%群号%==0 返回` 开头跳过私聊；私聊友好的规则反过来 `如果:%群号%!=0 返回`。

WebUI dispatcher 把每次请求合成成 DM scope（`kind="dm", id="0"`）：
- `%群号%` 解析为 `0`，`event.is_dm` 为 true，QQ 端 DM 和 WebUI 走同一条码路；
- 每账号独立，KV 状态不会跨账号污染；
- 操作员显式传 `scope_id="<某个群号>"` 时，把 scope 翻成 `kind="group", id=<override>`，可以测试只在某具体群里跑的规则。

**前端实现细节**：
- `POST /api/agents/<name>/chat` 请求体接 `scope_id`（可选）—— 带就用，不带就用 `webui:<account>`。
- WS `/ws/agents/<name>/stream` 的 `input` 帧接 `scope_id` 字段，同上。
- **Chat.vue 顶 bar 已加测试场景按钮**：圆形十字图标，点击弹出 sheet 让用户填群号，应用后所有后续消息都带这个 `scope_id`，按钮高亮表示当前在场景模式（值持久化在 `localStorage`）。

**KV 共享**：因为 `%QQ%` 在两端解析为相同字符串，规则中 `$读 小苏苏/灵玉 %QQ% 0$` 在 QQ 端写入的灵玉数，WebUI 端读得到（前提是规则真的运行了，即 `%群号%` gate 通过）。**`%群号%`-gated 的写入只在你的 scope 内可见**——如果在 QQ 群 A 写，从 WebUI 同账号读也只能看到 `webui:<account>` scope 下的值，不会看到群 A 的值。这是预期的隔离。

---

## 9. 兼容缺口（按优先级）

### P0 已交付

| 项 | 状态 | 测试 |
| :- | :-: | :-: |
| `%IMG0%` `%IMGN%` `%IMGNUM%` | ✅ | `test_imgnum_resolves_image_segment_count`、`test_img0_resolves_first_image_url` |
| `\%XX` URL-编码字面量 | ✅ | `test_url_escape_percent_xx_decoded_to_characters` |
| `##` 行注释 | ✅ | `test_double_hash_comment_skipped_in_body`、`test_double_hash_handler_dropped_at_parse_time` |
| `(?i)` flag 触发器 | ✅ | `test_qrspeed_inline_case_insensitive_trigger_matches` |

### P1 已交付

| 项 | 状态 | 测试 |
| :- | :-: | :-: |
| `%NDTime%` | ✅ | `test_ndtime_returns_milliseconds_now` |
| `%RobotRunTime%` | ✅ | `test_robotruntime_reflects_set_bot_start_time` |
| `%管理员%` `%主人%` | ✅ | `test_admin_resolved_from_extras`、`test_admin_empty_when_unconfigured` |
| `±ptt=URL±` voice | ✅ | `test_voice_sigil_emits_voice_segment` |
| `±fimg=URL±` flash image | ✅ | `test_flash_image_sigil_emits_image_with_extras` |
| `±rep msgid±` reply | ✅ | `test_reply_sigil_emits_reply_segment` |
| `±bub` `±strmsg` 软丢弃 | ✅ | `test_bub_and_strmsg_sigils_silently_dropped` |
| `$时间 fmt$` 任意 strftime | ✅ | `test_format_time_tool_translates_java_isms` |
| `$MD5 text$` | ✅ | `test_md5_tool_returns_hex_digest` |
| `$JSON 包含/键$` | ✅ | `test_json_contains_and_keys_subcommands` |
| **bracket-literal 触发器**（`[戳一戳]` `[系统]` `[退群]` 等）作为字面而非字符类 | ✅ | `test_bracket_trigger_matches_full_literal`、`test_bracket_trigger_does_not_match_inner_char` |
| WebUI ⇄ QQ 身份桥（`scope_id` 默认 = 合成 DM ``%群号%==0``） | ✅ | `test_webui_default_scope_is_dm_with_group_id_zero`、`test_webui_explicit_scope_id_can_target_main_group`、`test_webui_explicit_scope_id_overrides_default`、`test_webui_ws_input_accepts_scope_id_frame` |

### P2 待办（部分已完成 — 见下）

| 项 | 状态 | 说明 |
| :- | :-: | :- |
| `[系统]` `[退群]` `[上下管理]` 特殊触发器 | ✅ | OneBot adapter 把 notice / request 翻译为合成 message 事件；当规则集没注册对应 handler 时分类器返回 `ignore` 而不是兜到 LLM |
| `%UinName%` `%Inviteename%` `%Status%`（系统事件） | ✅ | OneBot adapter 在 `event.raw` 注入字段 |
| `$进群审核 group user A B reason$` | ✅ | 已注册并实现，调 OneBot `set_group_add_request` |
| `%FACE0%` `%FACENEW0%` `%FACEPRO0%`（消息内嵌表情） | ✅ | 索引 `FaceSegment.face_id` |
| `%XML0%` `%JSON0%`（卡片消息） | ✅ | 索引 `XmlSegment.xml` / `CardSegment.payload` |
| `%FIMG0%` 闪图 | ✅ | 索引带 `extras.flash=True` 的 `ImageSegment` |
| `$下载 path url$` | ✅ | 沙盒化文件下载（限 16MiB、httpx 走代理）；要求 `data_root` 配置 |
| `$图片链接 N$` | ✅ | 等价 `%IMGN%` 的工具形式 |
| `$群头像 group_id$` | ✅ | 返回 `https://p.qlogo.cn/gh/...` URL |
| `$管理员 user_id$` 查询 | ✅ | 检查 user_id 是否在 `admin_users` 配置里 |

### 永久放弃（明确不做）

| 项 | 原因 |
| :- | :- |
| `$BSH$` `$执行$` | 任意脚本注入风险 |
| `$访问 file:///path$`（任意磁盘读） | 文件存储走 KV，安全默认不开放 |
| `$词库操作$`（运行时改 AST） | 用 .ling 热重载替代，更安全 |

---

## 10. 自动化校验

| 工具 | 用途 |
| :- | :- |
| `tests/test_dicpro_corpus_audit.py` | corpus dry-run 全 public handler，断言 0 vm-error / 0 python-error |
| `tests/test_dsl_coverage.py` | 扫 dicpro.txt 全部 `$tool$` 引用，未注册 → CI 失败 |
| `scripts/audit_handlers.py` | 同样的 dry-run，输出 ok-emits / ok-silent / vm-error / no-input 分布 |
| `scripts/audit_dsl_coverage.py` | 工具状态 OK/STUB/MISSING |
| `scripts/audit_image_urls.py` | dicpro.txt 全部 `±img=...±` URL 可达性探测 |
| `packages/cli/tests/test_webui_chat_dispatch.py` | 端到端 WebUI 链路测试（含 scope_id 桥） |
| `packages/dsl/tests/test_vm_qrdic_compat.py` | QRSpeed 兼容回归测试（IMG / `\%XX` / `##` / `±ptt=` 等） |

跑法：
```bash
uv run pytest -q                                # 后端全套（742 tests）
uv run mypy                                     # 100 source files
uv run ruff check packages
uv run python scripts/audit_handlers.py         # corpus dry-run
uv run python scripts/audit_dsl_coverage.py     # 工具状态
uv run python scripts/audit_image_urls.py       # 图片 URL 可达性
pnpm --filter @linling/webui-frontend typecheck
pnpm --filter @linling/webui-frontend lint
pnpm --filter @linling/webui-frontend test:unit
```
