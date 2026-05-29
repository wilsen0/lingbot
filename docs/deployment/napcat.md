# 部署 NapCat（QQ 协议端）

linling 自己**不实现 QQ 协议**——`packages/adapters/onebot/` 只是一个 OneBot v11
WebSocket 客户端，需要外面有一个 OneBot 实现把 QQ 流量翻译过来。NapCat 是目
前最稳的选择（Lagrange / go-cqhttp 也行，配置思路一样）。

这份文档记录的是**当前生产机器实际用的那一套**（Docker + 桌面 QQ 注入），
所以下次新机器照抄就能跑。

## 一、整体拓扑

```
QQ 服务器
   │   QQ 私有协议（NapCat 注入桌面 QQ 后接管）
   ▼
┌─────────── docker container "napcat" ───────────┐
│  桌面 QQ (qq -q <ACCOUNT>) + NapCat 注入         │
│                                                  │
│  对外暴露:                                       │
│    :3001  OneBot v11 WebSocket 服务端            │
│    :6099  NapCat WebUI（看二维码 / 改配置）      │
└──────────────────────────────────────────────────┘
   │   ws://127.0.0.1:3001  (token: linling-secret-2026)
   ▼
linling (uv run linling run bot/bot.yaml --webui)
   ├─ adapters.onebot  →  连 NapCat
   ├─ adapters.cli     →  本机终端 REPL
   └─ webui :8787      →  浏览器观测面板
```

NapCat 是 **服务端**，linling 是 **客户端**——`bot.yaml` 里 `adapters.onebot.ws_url`
指向 NapCat 的 `:3001`，linling 启动时主动连过去。

## 二、当前机器实际配置

镜像：`mlikiowa/napcat-docker:latest`
Digest：`sha256:d8098fdabedfe5cbc570b994aebb685c3096a2bebbf4ea43ffb21655a4758e63`

容器：

| 项 | 值 |
| :- | :- |
| 容器名 | `napcat` |
| 重启策略 | `always` |
| QQ 账号 | `1707476110`（通过 `ACCOUNT` 环境变量传入） |
| 端口 | `127.0.0.1:3001 → 3001/tcp`（OneBot WS）<br>`127.0.0.1:6099 → 6099/tcp`（NapCat WebUI） |
| 卷 1 | `~/.napcat/QQ` ⇆ `/app/.config/QQ`（QQ 客户端登录态、会话缓存） |
| 卷 2 | `~/.napcat/config` ⇆ `/app/napcat/config`（NapCat 自己的配置） |

环境变量：

```text
ACCOUNT=1707476110     # 要登录的 QQ 号
WS_ENABLE=true         # 启用 OneBot WebSocket 服务端
TZ=Asia/Shanghai
```

> 端口绑在 `127.0.0.1` 而不是 `0.0.0.0` 是有意为之——OneBot 接口暴露给公网
> 等于把 QQ 账号交出去。要让另一台机器连，开 SSH tunnel 或反向代理，不要直接
> 改成 `0.0.0.0`。

## 三、首次部署（新机器）

### 1. 装 Docker

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 重新登录让组生效
```

### 2. 起 NapCat 容器

```bash
mkdir -p ~/.napcat/QQ ~/.napcat/config

docker run -d \
  --name napcat \
  --restart always \
  -e ACCOUNT=<你的QQ号> \
  -e WS_ENABLE=true \
  -e TZ=Asia/Shanghai \
  -p 127.0.0.1:3001:3001 \
  -p 127.0.0.1:6099:6099 \
  -v ~/.napcat/QQ:/app/.config/QQ \
  -v ~/.napcat/config:/app/napcat/config \
  mlikiowa/napcat-docker:latest
```

> 第一次跑容器需要先用 docker exec 启动 QQ，让它生成 `~/.napcat/config` 下面
> 那几个 JSON。如果直接照抄当前机器的配置文件（见下一节），可以跳过这步。

### 3. 写 OneBot WS 配置

NapCat 启动后会扫描 `~/.napcat/config/onebot11_<QQ号>.json`。复制下面这份，
把 `<QQ号>` / `token` 换掉：

```json
{
  "network": {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [
      {
        "enable": true,
        "name": "linling",
        "host": "0.0.0.0",
        "port": 3001,
        "reportSelfMessage": false,
        "enableForcePushEvent": true,
        "messagePostFormat": "array",
        "token": "linling-secret-2026",
        "debug": false,
        "heartInterval": 30000
      }
    ],
    "websocketClients": [],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
```

要点:

- `host: "0.0.0.0"` 是**容器内部**的监听地址，照写。对外的访问范围是
  上面 `docker run -p 127.0.0.1:3001:...` 控制的，不是这里。
- `messagePostFormat: "array"` —— linling 的 OneBot adapter 按 array 格式解析
  segment，**不要**改成 `string`。
- `reportSelfMessage: false` —— 否则 bot 会收到自己发的消息，触发回声循环。
- `token` 必须跟 `bot.yaml` 里 `${ONEBOT_TOKEN}`（即 `.env` 的 `ONEBOT_TOKEN`）
  一字不差。

改完文件**重启容器**让 NapCat 重新加载：

```bash
docker restart napcat
```

### 4. 登录 QQ

NapCat 第一次跑会要求扫码登录:

```bash
# 看二维码（推荐，最稳）
docker logs -f napcat | grep -A 30 "扫码\|二维码"

# 或者打开 NapCat WebUI（账户管理）
# 浏览器访问 http://127.0.0.1:6099
# 默认 token 在 ~/.napcat/config/webui.json 的 token 字段
```

登录态会写到 `~/.napcat/QQ/`（卷 1），下次重启容器免登。

如果你是远程服务器没办法直接看二维码：在本地用 SSH tunnel 把 6099 转回来：

```bash
# 本地终端
ssh -L 6099:127.0.0.1:6099 user@server
# 然后浏览器开 http://127.0.0.1:6099
```

### 5. 配 linling

`.env`：

```dotenv
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_TOKEN=linling-secret-2026
```

`bot/bot.yaml`：

```yaml
adapters:
  - kind: cli
  - kind: onebot
    ws_url: ws://127.0.0.1:3001
    access_token: ${ONEBOT_TOKEN}
```

启动:

```bash
uv run linling run bot/bot.yaml --webui --webui-port 8787
```

握手成功的标志是日志里出现：

```
onebot_ws_connected            ws_url=ws://127.0.0.1:3001
```

## 四、日常运维

### 看 NapCat 日志

```bash
docker logs --tail 200 -f napcat
```

### 重启 NapCat（账号掉线 / 配置改了）

```bash
docker restart napcat        # 或 ./start.sh 选 3
```

> ⚠️ 本机走移动 IP，重启后的「快速登录」基本会失败回退扫码（见末尾常见问题）。
> 想让重启能无人值守恢复，必须给容器配密码（见下面「免扫码自愈」）。
> 掉线**不要**第一反应清 `nt_qq*` 缓存——那只是强制重扫，治标且每次都要手动。

### 掉线自愈看门狗（推荐常驻）

`scripts/napcat_watchdog.sh` 定时用 OneBot `get_status` 检测在线状态，离线就：
**记录时间 + 当时公网出口 IP（取证用）→ `docker restart` → 靠容器里配的密码自动重登 → 复检**。
配了密码就全自动、不用扫码；没配密码时它只检测+记录，不会去清缓存（清缓存=强制
重扫，是人工动作）。

```bash
# 后台常驻（默认每 120s 检查一次）
./scripts/napcat_watchdog.sh --loop      # 或 ./start.sh 选 5

# 调轮询间隔
WATCH_INTERVAL=60 ./scripts/napcat_watchdog.sh --loop

# 或挂 cron（每 2 分钟）
*/2 * * * * /home/wilsen/apps/apps/linling/scripts/napcat_watchdog.sh >> /tmp/napcat_watchdog.log 2>&1
```

掉线取证日志默认写 `data/napcat_offline.log`，每行带时间戳和当时出口 IP——
攒几天可印证「IP 跳变 → 风控掉线」。连续 `NAPCAT_MAX_RESTARTS`（默认 2）次重启+密码
登录仍上不了线，看门狗会停手并提示人工 `./start.sh` 选 4。

### 免扫码自愈（配密码登录，本机推荐）

本机快速登录基本无效，**配密码是唯一能无人值守恢复的路**。给容器配登录密码后，
被踢下线时 NapCat 能用密码自动重登，配合看门狗做到零干预。给 `napcat` 容器加
环境变量（明文或 MD5 二选一）后**重建容器**（保留两个卷，登录态不丢）：

```bash
docker stop napcat && docker rm napcat
docker run -d --name napcat --restart always \
  -e ACCOUNT=1707476110 \
  -e WS_ENABLE=true \
  -e NAPCAT_QUICK_PASSWORD='<你的QQ密码>' \
  -e TZ=Asia/Shanghai \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  -v ~/.napcat/QQ:/app/.config/QQ \
  -v ~/.napcat/config:/app/napcat/config \
  mlikiowa/napcat-docker:latest
```

> 密码写在容器 env 里有泄露风险，注意 `~/.bash_history` / compose 文件的权限。
> 首次配密码后可能仍要先扫一次码完成设备授权，之后被踢就能密码自愈了。

### 升级 NapCat 镜像

```bash
docker pull mlikiowa/napcat-docker:latest
docker stop napcat && docker rm napcat
# 重新跑第三节那条 docker run
```

> 升级前最好把当前镜像 digest 记下来，方便回滚:
> `docker image inspect mlikiowa/napcat-docker:latest --format '{{.RepoDigests}}'`

### 强制下线再上线（账号被风控时）

> ⚠️ 这是「核武器」，只在账号被风控、软重连/看门狗反复救不回来时才用。
> 它会清掉登录缓存，**之后必须重新扫码**。日常掉线请用上面的软重连/看门狗。

```bash
docker exec -it napcat sh -c "rm -rf /app/.config/QQ/nt_qq*"
docker restart napcat
# 然后回到第 4 步重新扫码
```

> 这步会清掉登录缓存，**不要**清 `/app/napcat/config`，否则 OneBot 配置丢失。

## 五、常见问题

**账号反复掉线，日志 `[KickedOffLine] 你的帐号当前登录已失效` + 偶尔弹 `ti.qq.com/safe/.../sms-verify-login`**

实测根因（这台机器）：**出口走中国移动蜂窝 CGNAT，公网 IP 频繁跳变**
（`223.104.x.x / 广东东莞 移动`），QQ 风控把注入式 PC 端登录判成「异地异常」，
周期性强制下线、并要求短信验证。佐证：

- 掉线间隔不规律（3 小时~3 天），不是固定 TTL，排除「服务端定期过期」。
- 8 次掉线里 7 次前 10 分钟 bot 一条没发，排除「刷屏被限流」。
- 日志出现 `ti.qq.com/safe/tools/captcha/sms-verify-login` —— 这是安全中心风控验证。
- 设备管理列表只有 Linux 一台，排除「第二个 PC 端互踢」。

**为什么「软重启走快速登录」对本机无效**：快速登录靠服务端下发的短期 ticket，
本机要么写不进（`nt_qq/global/nt_data/Login/.<QQ>` 为 0 字节），要么因 IP 变了
被判 `登录态已失效`。所以重启后基本都回退扫码——这就是为什么每次都得清缓存重扫。

**正确的修复方向**（按有效性）：

1. **换固网/有线出口**（治本）：IP 稳定、归属地固定，异地信号消失。别再走手机
   USB 共享 / 热点的蜂窝流量（两者出口同一张 SIM，换热点不解决问题）。
2. **配密码自动登录**（止血、可无人值守）：给容器配 `NAPCAT_QUICK_PASSWORD`，
   被踢后 `scripts/napcat_watchdog.sh` 自动重启 + 密码重登，不用扫码。
3. **实在救不回**：`./start.sh` 选 4 清缓存重扫（会给出 WebUI 🔗 + 终端二维码）。

> 不要把「清 `nt_qq*` 缓存」当日常重连手段——它只是强制重扫，治标且每次都要你
> 动手。真正决定能否上线的是出口网络和密码，不是缓存。

**linling 一直连不上，日志只有 `onebot_ws_connecting` 没有 `_connected`**

- `docker ps` 看 napcat 容器是不是 `Up`
- `curl -i -H "Authorization: Bearer <token>" -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: x" http://127.0.0.1:3001/`
  ——返回 `101` 是握手成功，返回 `401` 是 token 不对，连接被重置一般是
  `websocketServers[0].enable` 为 false 或端口没开
- token 在 `bot.yaml` / `.env` / `onebot11_*.json` 三处必须**完全一致**

**收到消息但发不出去 / 一直超时**

- 大概率是消息里带了**死链图片** URL，NapCat 在 fetch 阶段卡 TLS。看
  `scripts/strip_dead_image_urls.py` 的逻辑，本地 `picture/*.jpg` 不会触发。
- `core/config.py` 里 `session_lock_wait_s` 默认 10s，超过这个值 router 会
  放掉这条事件。日志里搜 `session_lock_wait` 能看到。

**收到 notice / request 但 DSL handler 不触发**

- linling adapter 把 notice / request 翻译成合成 message 事件
  （`[系统]` `[退群]` `[上下管理]`），见 `docs/dsl/grammar.md` 的特殊触发器表。
- 对应规则没写就是 `verdict=ignore`，不是 bug。

**WebUI（6099）打不开 / 不认 token**

- token 在 `~/.napcat/config/webui.json` 的 `token` 字段，第一次启动随机生成。
- 改了 `webui.json` 必须 `docker restart napcat`。

## 六、为什么用 Docker 而不是直接装

- **隔离 root 写**——NapCat 注入需要修改 `/opt/QQ/resources/app/`，宿主机
  装会污染 QQ 安装目录，升级 QQ 时容易冲突。容器内改的是镜像里那份 QQ。
- **日志 / 重启 / 升级都有标准动作**——`docker logs` `docker restart`
  `docker pull`，不用自己写 systemd unit。
- **撤掉很干净**——`docker rm napcat && rm -rf ~/.napcat` 就回到出厂。

如果你坚持装在宿主机，参考 NapCat 上游文档:
<https://napneko.github.io/>。装完之后 `bot.yaml` 那段配置完全一样，只是
`ws_url` 可能要改成宿主机能访问的地址（`127.0.0.1:3001` 通常够用）。
