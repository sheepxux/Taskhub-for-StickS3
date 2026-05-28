# StickS3 语音备忘录 · 设计文档 v0.1

> 一个基于 M5StickS3 + OpenClaw 的随身语音记录器。
> 按一下说话,回家自动同步,本地转录,每天 22:00 用 LLM 摘要并推送到手机和设备。

---

## 1. 项目概述

### 是什么

一根可以随身携带的语音棒。想记什么的时候按住主按钮说话,松开自动保存。
回到家 Wi-Fi 范围内自动上传到家里 Mac Mini 上跑的 OpenClaw,
本地用 whisper.cpp 转录,LLM 生成标题和摘要。
每天晚上 22:00 把当日总结推送到手机 IM(Telegram/iMessage)和设备屏幕。

### 不是什么

- 不是语音助手(不做即时问答交互)
- 不是闹钟、计时器、遥控器
- 不依赖任何云 ASR 服务

### 解决的核心问题

灵感、想法、待办在脑子里转瞬即逝。掏手机解锁打字摩擦大。
现有的语音备忘工具(手机自带)做不到主动每日总结。

### 核心特性

1. **极低摩擦录入**:push-to-talk,几十秒说完就完
2. **离线可用**:本地缓存,回家自动同步
3. **隐私优先**:录音永不离开你的本地网络
4. **主动总结**:每天 22:00 用 LLM 聚合摘要并主动推送
5. **长期记忆**:内容存为 Markdown,被 OpenClaw memorySearch 自动索引

---

## 2. 系统架构

```
┌─────────────────┐         ┌──────────────────────────────┐
│   M5StickS3     │         │      Mac Mini M4 (家)        │
│                 │   WiFi  │                              │
│ ┌─────────────┐ │ ◄─────► │ ┌──────────────────────────┐ │
│ │  Firmware   │ │  HTTPS  │ │       OpenClaw           │ │
│ │ (Arduino)   │ │         │ │                          │ │
│ │             │ │         │ │ ┌──────────────────────┐ │ │
│ │ - Mic/I2S   │ │         │ │ │  voice-recorder      │ │ │
│ │ - Opus enc  │ │         │ │ │       skill          │ │ │
│ │ - Flash buf │ │         │ │ │                      │ │ │
│ │ - State M.  │ │         │ │ │ - server.py          │ │ │
│ │ - Display   │ │         │ │ │ - transcribe.py      │ │ │
│ └─────────────┘ │         │ │ │ - summarize.py       │ │ │
│                 │         │ │ │ - notify.py          │ │ │
└─────────────────┘         │ │ └──────────┬───────────┘ │ │
                            │ └────────────┼─────────────┘ │
                            │              │               │
                            │   ┌──────────▼───────────┐   │
                            │   │   whisper.cpp        │   │
                            │   │  (本地, large-v3)    │   │
                            │   └──────────────────────┘   │
                            └──────────────┬───────────────┘
                                           │
                                ┌──────────▼──────────┐
                                │  IM (Telegram /     │
                                │  iMessage / 微信)   │
                                └─────────────────────┘
```

数据流:
1. StickS3 录音 → Opus 编码 → Flash 缓存
2. Wi-Fi 上线 → 上传到 OpenClaw skill server
3. server 把音频塞队列 → transcribe worker 调 whisper.cpp 转录
4. 转录结果 + LLM 生成标题 → 写入 memory/voice/YYYY-MM-DD.md
5. 每天 22:00 → summarize 调 LLM 聚合 → notify 推送到手机 + StickS3

---

## 3. 硬件: M5StickS3

### 关键规格

| 项 | 规格 |
|---|---|
| 主控 | ESP32-S3-PICO-1-N8R8 |
| Flash / PSRAM | 8 MB / 8 MB |
| 屏幕 | 1.14 寸 IPS, 240×135 |
| 麦克风 | MEMS,ES8311 codec |
| 喇叭 | 1 W,AW8737 功放 |
| IMU | 6 轴 |
| 红外 | 收发都有 |
| 电池 | 250 mAh 锂电 |
| 接口 | USB-C OTG, Grove, Hat2-Bus 16P |

### 本项目用到的硬件

- ✅ 麦克风(核心:录音)
- ✅ 喇叭(提示音、通知到达)
- ✅ 屏幕(状态显示、通知正文)
- ✅ 按钮 BtnA、BtnB(用户交互)
- ✅ Wi-Fi(上传、拉通知)
- ✅ Flash(音频本地缓存)

### 本项目暂不用的硬件

- ❌ 红外、IMU、Grove、Hat2-Bus、BLE——可作为后续扩展

---

## 4. 按钮交互

### 物理按钮

| 按钮 | 位置 | 可识别事件 |
|---|---|---|
| BtnA | 正面 M5 logo 下方 | press / release / click / hold / double-click 完整 |
| BtnB | 右侧 | 同上,完整 |
| BtnPWR | 左侧电源键 | 仅 wasClicked / wasHold(PMIC 限制) |

### 映射

| 按钮事件 | 在何种状态 | 动作 |
|---|---|---|
| BtnA 按下瞬间 | IDLE | 立刻进入 RECORDING(不等 hold 判定) |
| BtnA 松开,按住 ≥ 150 ms | RECORDING | 保存录音 → SAVED |
| BtnA 松开,按住 < 150 ms | RECORDING | 丢弃 → 显示 IDLE 状态信息 |
| BtnB 短按 | IDLE | 进入 REVIEW(若有今日条目) |
| BtnB 短按 | REVIEW | 显示更老一条 |
| BtnB 双击 | REVIEW | 进入 DELETE_CONFIRM |
| BtnB 双击 | DELETE_CONFIRM(2 s 内)| 确认删除 → 返回 REVIEW |
| BtnB 长按 | IDLE | 强制同步(若 Wi-Fi 可用) |
| BtnB 长按 | REVIEW | 退出 → 返回 IDLE |
| BtnPWR | 任何 | 系统行为(开关机),firmware 不重写 |

### 关键判定

- 按下立即录音(不等 hold 判定),避免说话开头被吃掉
- 150 ms 阈值区分"短按看状态"和"录音"

---

## 5. UI 设计

### 设计原则(v1,锁定)

- 全黑背景,IPS 工具感
- 多色编码状态:白(主信息)、绿(已同步)、琥珀(活跃/待办)、红(破坏性)、蓝(通知)
- 等宽字体给时间和数字,无衬线给中文
- 状态图标(电池、Wi-Fi)只在 IDLE 屏出现

### 6 个屏幕状态

#### IDLE 待机
- 顶部:14:32 + Wi-Fi 图标 + 电池图标 + 87 %
- 中央:大字 `7` + 小字 `今日已记`
- 底部:绿点 + `已同步`

#### RECORDING 录音中
- 顶部:琥珀 `● 录音中`
- 中央:琥珀脉冲圆 + 大字 `0:08`
- 底部:17 根琥珀音量条 + `松开停止`

#### SAVED 已保存(1.5 s 后回 IDLE)
- 中央:绿圆 + 白对勾 + `已记 0:12`
- 底部:琥珀点 + `待同步 N 条`

#### REVIEW 翻看
- 顶部:`3 / 7` + `今天 14:32`
- 中央:标题 + 三行预览
- 底部:`BtnB 上一条 · 长按退出`

#### DELETE_CONFIRM 删除确认(2 s 倒计时)
- 顶部:红色警告三角 + `!`
- 中央:`再次双击删除` + `这条无法恢复`
- 底部:红色进度条(线性回退) + `2 秒内无操作则取消`

#### NOTIFICATION 通知
- 顶部:蓝铃铛 + `晚间总结 · 22:00`
- 中央:标题 + 正文
- 底部:`BtnB 标记已读`

### 动画

- 录音中:琥珀脉冲圆每秒一次(两层外环淡入淡出),音量条按真实音量跳动
- 已保存:绿对勾从 0 弹出(200 ms 弹性曲线)
- 删除确认:底部进度条线性回退 2 秒

---

## 6. 状态机 + 电源管理

### 核心原则

默认深睡,按需唤醒,任何活动状态不超过 30 秒。

### 状态超时表

| 状态 | 默认超时 | 下一状态 |
|---|---|---|
| DEEP_SLEEP | ∞ | 按键 / 定时唤醒 → IDLE |
| IDLE | 8 s | DEEP_SLEEP |
| RECORDING | 用户控制 | SAVED / IDLE |
| SAVED | 1.5 s | IDLE |
| REVIEW | 30 s | IDLE |
| DELETE_CONFIRM | 2 s | REVIEW(不删除) |
| SYNCING | 上传完成 | IDLE |
| NOTIFICATION | 20 s | IDLE |

### 转移触发器

- DEEP_SLEEP → IDLE:BtnA / BtnB 按下,或每小时一次的定时唤醒(合并 NTP + 通知 pull)
- IDLE → RECORDING:BtnA 按下瞬间
- IDLE → REVIEW:BtnB 按下且今日有条目
- IDLE → SYNCING:检测到 Wi-Fi 上线 + 本地有待传条目
- IDLE → NOTIFICATION:唤醒时 pull 到未读,或上传响应携带通知

### 功耗 profile(随身典型使用画像)

| 项目 | 时长/天 | 平均电流 | 日消耗 |
|---|---|---|---|
| RECORDING(20 次 × 30 s)| 10 min | 90 mA | 15 mAh |
| IDLE 查看(25 次 × 8 s)| 3.3 min | 50 mA | 2.8 mAh |
| REVIEW(3 次 × 30 s)| 1.5 min | 50 mA | 1.25 mAh |
| SYNCING(1 次 × 20 s)| 0.3 min | 150 mA | 0.83 mAh |
| DEEP_SLEEP | ~23 h | 50 μA | 1.15 mAh |
| **合计** |   |   | **~21 mAh** |

250 mAh 电池扣掉 Wi-Fi 重连损耗和电池余量,**实测预期 1–1.5 天一充**。
睡前充电模式可行。

### 工程关键点

- 录音时屏幕亮度降到 50 %,省约 1.7 mAh/天
- 深睡用 EXT0 GPIO 唤醒,合并 NTP 校时和通知 pull 为每小时一次
- Flash 写入用追加模式,8 MB 寿命远远够用
- 同步用 HTTP keepalive,避免每次重连 Wi-Fi 浪费 5 秒/100 mA

---

## 7. API 规范

### 部署

OpenClaw 端起本地 HTTPS 服务,监听 `127.0.0.1:5577`,自签证书。
StickS3 作为 client,证书烧到 NVS。

### 鉴权

device token (PSK) 烧到 StickS3 NVS。每个请求带:

```
X-Device-Token: <token>
```

OpenClaw 端检查白名单。家庭内网够用,不上 OAuth。

### 端点

```
POST   /api/v1/entries                  上传录音
GET    /api/v1/entries/today            今日列表
DELETE /api/v1/entries/{id}             删除某条
GET    /api/v1/notifications/unread     未读通知列表
POST   /api/v1/notifications/{id}/ack   标记已读
```

### Schema

#### POST /api/v1/entries

Request: `multipart/form-data`

| 字段 | 类型 | 说明 |
|---|---|---|
| audio | binary | Opus 编码 |
| recorded_at | string | ISO 8601 with timezone |
| device_id | string | 烧到 NVS |
| duration_ms | int | 录音时长 |
| client_seq | int | StickS3 本地累加,用于幂等 |

Response 200:

```json
{
  "entry_id": "ent_2026_05_27_xxxxxx_yyyyyy",
  "status": "queued"
}
```

#### GET /api/v1/entries/today

Response 200:

```json
[
  {
    "id": "ent_...",
    "recorded_at": "2026-05-27T14:32:11+08:00",
    "duration_ms": 12340,
    "title": "咖啡馆灵感",
    "preview": "想到一个产品点子——把..."
  }
]
```

#### GET /api/v1/notifications/unread

Response 200:

```json
[
  {
    "id": "ntf_...",
    "kind": "daily_summary",
    "title": "今天记了 7 条",
    "body": "产品点子(3) · 阅读笔记(2) · 待办(2)。详情已推送到 Telegram。"
  }
]
```

### 音频格式

Opus @ 16 kHz mono, 16 kbps。一分钟约 120 KB。

- ESP32-S3 有 libopus 移植,CPU 占用低
- 16 kbps 对语音够用(VoIP 标准)
- whisper.cpp 接 Opus 需先转 PCM(用 ffmpeg)

### 故障处理

- 上传失败:StickS3 保留本地文件,下次 Wi-Fi 上线重试
- 重试 5 次仍失败:IDLE 屏右上角显示 `sync error`
- 手动同步:IDLE 时 BtnB 长按

---

## 8. 后端: OpenClaw skill

### 部署

跑在 Mac Mini M4,路径 `~/.openclaw/skills/voice-recorder/`。

### 文件结构

```
~/.openclaw/skills/voice-recorder/
├── SKILL.md                # OpenClaw 用来发现的元信息
├── server.py               # HTTPS server, 5 个 endpoints
├── transcribe.py           # 异步 worker: Opus → ffmpeg → whisper.cpp → MD
├── summarize.py            # cron 22:00: 当日条目 → LLM → summary.md
├── notify.py               # 推送到 IM + StickS3
├── state.db                # SQLite: 转录队列, 通知, 同步状态
├── bin/
│   └── whisper-server      # whisper.cpp 编译产物
├── models/
│   └── ggml-large-v3.bin   # ~3 GB
├── audio_uploads/          # Opus 暂存,转录完后删除
└── memory/
    └── voice/
        ├── 2026-05-27.md           # 当日所有转录
        └── 2026-05-27-summary.md   # 当日 22:00 摘要
```

### 各组件职责

#### server.py

- 5 个 HTTP endpoints 的实现
- 收到上传后立刻 200 OK,把任务塞 SQLite 队列
- 异步:StickS3 不等转录完成,可以立刻回深睡

#### transcribe.py

后台 worker,从队列拉录音文件:

1. `ffmpeg -i {entry_id}.opus -ar 16000 -ac 1 -f wav -` → 16 kHz PCM
2. POST 到 `http://127.0.0.1:8080/inference`(whisper-server)
3. 拿回 text,用 LLM 生成 5-8 字标题(同一次 LLM 调用)
4. 追加到 `memory/voice/YYYY-MM-DD.md`:

   ```markdown
   ## 14:32 · 咖啡馆灵感
   
   想到一个产品点子——把本地 LLM 跑在路由器上...
   ```

5. 删 audio_uploads 里的 Opus 文件
6. 更新 SQLite 状态为 `done`

#### summarize.py

每天 22:00 cron 触发:

1. 读 `memory/voice/YYYY-MM-DD.md`
2. 调 LLM 分类聚合(产品点子 / 阅读笔记 / 待办 / 其他)
3. 写到 `memory/voice/YYYY-MM-DD-summary.md`

#### notify.py

summarize 完成后调用:

1. 把 summary 推到配置的 IM channel(Telegram bot / iMessage / 微信)
2. 同时 POST 一份摘要到 StickS3 的 inbound endpoint
3. SQLite 标记一条 unread notification

### 与 OpenClaw memory 集成

- `MEMORY.md`:用户长期偏好(不动)
- `memory/voice/YYYY-MM-DD.md`:OpenClaw Tier 2 daily context
- summary 文件被 OpenClaw `memorySearch` 自动索引

未来用户在 OpenClaw 聊天里问"上周关于 LLM 的想法",
semantic search 直接命中相关条目。

---

## 9. 关键决策记录(ADR)

### ADR-001:录音用 Push-to-Talk 而非 Toggle

**决策**:按住 BtnA 录音,松开停止。

**理由**:
- 用户的备忘场景主要是几十秒的短想法
- Toggle 容易误录(按完忘了停,录到隐私对话或塞满 Flash)
- Push-to-talk 物理上不可能犯"忘了在录"的错

**放弃方案**:
- Toggle 模式——误录风险高
- 混合模式(按住 2 分钟自动转 Toggle)——增加复杂度,对短录音场景无好处

### ADR-002:本地 whisper.cpp 而非 OpenAI API

**决策**:用 whisper.cpp + large-v3,跑在 Mac Mini M4。

**理由**:
- 隐私:语音备忘可能含个人/情绪/工作敏感内容,不发到云端
- 性能:M4 + Core ML 加速,30 秒录音 1-2 秒转完
- 成本:长期免费

**放弃方案**:OpenAI Whisper API(约 $22/年)——隐私代价不值得这个钱。

**反思**:初版评估把"$22/年"作为权衡中心是错的——
让容易量化的小数字盖过了不容易量化但更重要的隐私维度。

### ADR-003:本地缓存 + 回家同步,而非随身实时上传

**决策**:录音存 Flash,Wi-Fi 上线时批量上传。

**理由**:
- StickS3 没 4G/5G,不能独立联网
- 走 BLE 中继手机复杂度极高(需手机 app 或快捷指令配合)
- 用户"想记就记"对同步时机不敏感

**放弃方案**:BLE 中继手机上传。

### ADR-004:状态图标只在 IDLE 屏显示

**决策**:仅 IDLE 屏显示电池和 Wi-Fi,默认隐藏 Wi-Fi 图标;
异常时(offline / sync error)以小琥珀字回到屏幕。

**理由**:
- 240×135 屏空间稀缺,主信息优先
- 异常状态比正常状态更值得占用注意力("无消息即好消息")

### ADR-005:删除操作放到 REVIEW 中,不放 BtnA

**决策**:翻看中 BtnB 双击 + 2 s 倒计时确认。

**理由**:
- BtnA 留给最高频的录音相关操作(按下立即录、松开判断)
- 删除前必须先进入翻看,物理上几乎不可能误触
- 2 s 倒计时给反悔窗口

**放弃方案**:BtnA 双击删除——与"BtnA 短按看状态"事件冲突,
需要 `wasDecideClickCount` 等待,造成 500 ms 延迟。

### ADR-006:UI 颜色保持 v1 多色,不走极简单色

**决策**:绿/琥珀/红/蓝/白/灰六色编码状态。

**理由**:
- v2 极简单色版本砍掉绿色后,无法一眼看出"已同步"
- 多色实际上是在工作:每种颜色对应一种状态语义,少一个就少一份即时信息
- 工具型设备应优先功能性,而非美学

**反思**:不要因为"看上去更高级"就砍掉承载信息的颜色。
极简的代价是信息密度。

### ADR-007:暂不做 Opus,改用 WAV + 扩大数据分区

**决策**:固件录音保持 16kHz/16bit WAV;通过自定义分区表(砍掉第二个 OTA app 槽)把数据分区从 1.5MB 扩到 4.52MB(≈141s),覆盖离线缓存需求。Opus 编码(原 Step 5b)降级为"暂不做"。

**理由**:
- 真实需求小:用户出门单次录音 ~1 分钟封顶。1 分钟 WAV=1.92MB,扩分区即可,无需压缩。
- 电池是真瓶颈,存储不是:设备仅 250mAh。Opus 是实时编码,录音时持续吃 CPU;而录音状态本就是耗电大头。为省"不缺的存储"去烧"稀缺的电",方向错了。WAV 录音近乎只有 I2S DMA + Flash 写,最省电。
- 复杂度/风险:可用的 ESP32 Opus 库只产裸帧、无 Ogg 容器,需自定义封装并改后端解码,且稳定性需实测。WAV 经 ffmpeg 按内容解码,后端零改动。

**放弃方案**:
- 现在就上 Opus——复杂度与耗电不匹配 1 分钟的真实需求。
- 维持 1.5MB 默认分区——只有 ~45s,不够 1 分钟。

**反思**:原设计把 Opus 当默认前提,是在"未量化真实使用画像"时定的。一旦明确"单次 ~1 分钟 + 电池极小",约束的优先级就翻转了——稀缺的是电与简单度,不是字节。先问场景,再选格式。

---

## 10. 实施路线图

每个 Step 都有明确产出和验收标准,完成后才进下一步。

### Step 1:OpenClaw skill 骨架(假数据)

**产出**:
- `server.py` 跑起来,5 个 endpoints 返回假数据
- 无 TLS、无鉴权、in-memory 状态

**验收**:6 个 curl 命令全部返回预期 JSON 格式。

**状态**:✅ 完成。6 个 curl 流程全绿(含幂等、404 边界)。

### Step 2:接入 whisper.cpp 做真实转录

**产出**:
- whisper.cpp 编译 + large-v3 模型下载
- whisper-server launchd plist 开机自启
- `transcribe.py` 把上传的 Opus 转成 Markdown 写入 memory/

**验收**:用任意 Opus/WAV 文件 POST 上来,5 秒内 `memory/voice/YYYY-MM-DD.md` 出现转录条目。

**状态**:✅ 完成(代码与管线)。SQLite(WAL)持久化 + 音频落盘;transcribe.py 双后端(`whisper-cli` 直调 / 常驻 `whisper-server`,后者经 `WHISPER_SERVER_URL` 启用);whisper-cli + ffmpeg 已装(Metal 加速)。用 base 模型端到端实测 ~0.7s 出条目,远低于 5s。large-v3(2.95GB)下载中,完成后切 `WHISPER_MODEL` 即用。

### Step 3:summarize + notify(22:00 链路)

**产出**:
- `summarize.py` 跑 fake 数据生成 summary.md
- `notify.py` 推到一个 IM channel(建议 Telegram bot,最易接入)
- cron 任务接入

**验收**:手动触发 summarize,摘要文件生成 + 手机收到推送。

**状态**:✅ 完成(代码与本地链路)。summarize.py 分类聚合写 `*-summary.md`(Claude API,无 key 时退关键词启发式,实测 6 条分类正确);notify.py 写 SQLite 未读通知(设备可拉)+ Telegram 推送;`run_daily.sh` + launchd plist 接 22:00。实测 summarize→notify→`GET /notifications/unread` 全通。**待用户提供凭据激活**:`ANTHROPIC_API_KEY`(LLM 摘要)、`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`(推送)——托管环境屏蔽了 host 的 key,故 LLM/Telegram 实活路径未实测。

### Step 4:StickS3 firmware 基础

**产出**:
- Arduino + M5Unified 工程搭建
- 状态机骨架:6 个状态 + 转移逻辑
- 按钮事件处理:BtnA push-to-talk, BtnB review
- 6 个屏幕 UI 绘制

**验收**:不录音也能完整跑通所有状态转移,屏幕显示正确。

**状态**:✅ 完成并实测。arduino-cli + esp32@3.3.8 + M5Unified 编译过(56% flash / 7% RAM);烧录到实机,6 个屏幕渲染正确,所有状态转移(录音/保存/丢弃/翻看/删除确认/同步/深睡唤醒→通知)逐项跑通。修复:REVIEW 里「下一条」由 `wasClicked` 改 `wasSingleClicked`,消除单击与双击删除的冲突(代价:翻页响应延迟约半个双击窗口,可接受)。

### Step 5:录音 + Opus 编码 + Flash 缓存

为降风险拆成两小步(用户确认):先 WAV 跑通麦克风+Flash 通路,再加 Opus。

**产出**:
- ES8311 麦克风初始化
- LittleFS 文件系统 + 录音文件写入
- (5a)WAV 写入;(5b)libopus 编码

**验收**:录一段,从 Flash 拷出音频文件,Mac 上 vlc 能播放,音质清晰。

**Step 5a 状态**:✅ 完成并实测。M5.Mic(16kHz/mono/16bit)双缓冲流式写 WAV 到 LittleFS(`/rec/NNNN.wav`,seq 存 NVS);录音中音量条改读真实麦克风峰值。实机录两条,esptool 读 spiffs 分区 + mklittlefs 解包拷出,VLC 实听清晰、格式校验 mono/16bit/16kHz 正确。已知:M5Unified 默认麦克风增益 `magnification=16` 会让大声处 peak 顶满(削顶),当前可听清,后续若影响 whisper 准确率再调低。

**Step 5b 状态**:⏸ 降级为暂不做(见 ADR-007)。用户真实场景出门单次录音 ~1 分钟封顶 + 电池仅 250mAh,改用「砍掉 OTA 冗余、扩大数据分区」让 WAV 离线缓存到 ~2.3 分钟,既覆盖需求又避免 Opus 实时编码的 CPU/耗电与依赖风险。自定义分区表见 `firmware/voice_recorder/partitions.csv`:单 app 3.5MB + 数据区 4.52MB(≈141s WAV)。

### Step 6:Wi-Fi + 上传(端到端)

**产出**:
- Wi-Fi station mode + 自动重连
- HTTP client + 上传 + 重试逻辑(HTTPS/token 见下,本步用明文)

**验收**:真正按下 BtnA → 录 → 松开 → 自动上传 → 几秒内 memory/voice/ 出现转录。

**状态**:✅ 完成并真机实测。固件:WiFi station + 自动重连、NTP(CST-8)校时、保存时写 `.meta` sidecar 存真实录音时间、`uploadAll()` 把 `/rec/*.wav` 以 multipart 上传(成功即删本地、`pendingSync` 实时反映),SAVED 后联网自动同步 / IDLE 长按 BtnB 手动同步;离线则留本地待回家补传。后端:server.py 改绑 `0.0.0.0:5577`(`VR_HOST`/`VR_PORT` 可覆盖),WAV 经 ffmpeg 按内容解码无需改动。实测设备(192.168.110.138)上传 `client_seq=3` → whisper 转出「提醒我明天早上買牛奶」、`recorded_at` 为正确 NTP 时间、写入当日 md,屏幕显示「已同步」。**简化决策**:家庭内网用明文 HTTP、无鉴权(token 发了但 server 暂忽略),设计 §7 的 HTTPS+PSK 留作后续加固。凭据在 `secrets.h`(gitignore)。

### Step 7(可选):摘要推回 StickS3 + 完整闭环

**产出**:
- 通知拉取逻辑
- NOTIFICATION 屏渲染

**验收**:22:00 后按 BtnA 唤醒,屏幕显示当日摘要,喇叭响一声。

---

## 11. 已知限制 / 不做的事

- **续航**:1–1.5 天一充。不做"3 天续航"——需要更大电池或外置电源
- **远场拾音**:内置麦克风需贴近说话(< 50 cm)。不做远场降噪
- **出门在外**:不能实时上传,必须等回家 Wi-Fi。不做手机 BLE 中继(复杂度太高)
- **语音指令**:只录音,不做"小助手"式问答交互
- **多设备**:假定一个用户一根棒。不做多设备同步
- **共享/导出**:不做"导出某条到 X 平台"。所有数据留在你的 Mac

---

## 12. 后续可能扩展(不在 v0.1 范围)

- IMU 手势触发录音(拿起设备就开始录)
- 红外发射做语音控空调(用上 IR 硬件)
- BLE 中继手机做出门也能传
- 多语言混合输入
- 离线唤醒词("Hey 小记")
- 摘要分类用户自定义

---

## 13. 工作约定(根据用户偏好)

- 设计先于实施,每个 Step 必须有明确验收标准
- 任何用户纠正都更新到 `tasks/lessons.md`(见 ADR-002、ADR-006)
- 改动最小影响面,不重构没坏的东西
- 完成前必须用事实(测试、日志)证明可运行,不只是 hope it works

---

**文档版本**:v0.1  
**当前阶段**:Step 1–6 完成并验证(端到端真机闭环已通:按键→录音→WAV→上传→whisper 转录→memory/voice)。Step 5b(Opus)按 ADR-007 暂不做;Step 7(摘要推回设备)可选,待开始。  
**最后更新**:2026-05-28
