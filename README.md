<div align="center">

# MaiBot Discord Adapter

<p>
  <a href="https://discord.gg/KArcrcdWVt">
    <img src="https://img.shields.io/badge/Discord-Maiwithu-5865F2?style=flat&logo=discord&logoColor=white" alt="Discord">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License">
  </a>
</p>

MaiBot 的 Discord 平台适配器插件，让你的 MaiBot 能够在 Discord 上运行。

> [MaiBot 原仓库](https://github.com/Mai-with-u/MaiBot)

</div>

---

## 当前能力

- 入站消息
  - Guild 频道消息
  - DM 私聊消息
  - Thread 子区消息
  - 提及、引用回复、图片、贴纸
  - 圖片連結、動圖連結（Klipy、Tenor、Giphy）與網頁卡片的預覽圖（等 Discord 展開預覽後當圖片交給識圖和圖片記憶）
  - 影片按固定秒数截几格画面拼成一张图交给识图（需要 FFmpeg，直接读 Discord CDN 的片段，不整个下载）
  - Raw reaction add/remove 事件
  - 可选语音转文本 STT
- 出站消息
  - 普通文本
  - 引用回复
  - 用户/角色提及
  - 图片与部分附件
  - Reaction add/remove
  - DM / Guild / Thread 路由
  - 可选文本转语音 TTS
- 运行时能力
  - WebUI / `config.toml` 配置映射
  - 连接状态上报
  - 断线重连与健康检查
  - 子区上下文路由
  - Discord 平台消息 ID 回执回写

---

## 快速开始

1. 将插件放到 `MaiBot/plugins/maibot-discord-adapter`
2. 启动 MaiBot，让宿主加载插件
3. 在 WebUI 或插件配置中启用插件
4. 填写 `connection.token`
5. 如果要启用语音，同时打开 `voice.enabled` 和 `connection.intent_voice_states`
6. 按场景填写 `voice.fixed_channel_id` 或 `voice.auto_channel_list`

详细配置见：

- [安装配置指南](./docs/setup_guide.md)

---

## 当前语音提供方

TTS:

- `siliconflow`
- `gptsovits`
- `minimax`
- `fishaudio`

STT:

- `siliconflow`
- `aliyun`
- `tencent`
- `gemini`（Gemini Live API，默认 `gemini-3.5-transcribe-live`）

语音功能的最低前置条件：

- `voice.enabled = true`
- `connection.intent_voice_states = true`
- Bot 对目标语音频道拥有 `View Channel`、`Connect`、`Speak` 权限

- [语音配置指南](./docs/voice_config_guide.md)

---

## 决策模型与繁体输出

设置页「聊天与过滤」标签里：

- **決策模型**：接 [vLLM-SR Decision](https://app.vllm-sr.ai)（有免費額度）或 [Jev](https://api.typesafe.ai) 這類只回機率的模型，兩家都是 `POST /v1/systemone`，填 API 位址、Key 和模型名就能切換。收到新訊息後第一次進 planner 時，拿 planner 這輪看到的同一份群聊（最後 8 則別人的訊息當可以表態的對象，附上別人已經按的表情），問它這輪該用「只要文字」「文字＋附圖」「只按表情」還是「只發圖」參與、最值得表態的是哪一則（不一定是最新一則）、要表達什麼情緒；發圖的對象太早時改成按在那則上；結果只用一句提示告訴 planner，不會拿掉它的工具（拿掉時 planner 會把回覆直接寫在自己的正文裡，打亂 reply / replyer 的流程）；唯一例外是 Discord 聊天裡拿掉 MaiBot 原生的 `send_emoji`，發表情包一律用 `discord_emote`（它的貼圖也從表情庫挑），免得兩套決定。只按表情／只發圖會直接替她做，並提醒 planner 沒人在問她的話這輪就不用再說話（MaiBot 不讓插件跳過 planner）；「有沒有人在叫她」和 MaiBot 的被提及判斷一致：@ 她、回覆她、私訊，或提到 `bot.nickname` / `bot.alias_names` 裡的名字；文字＋附圖會等她的回覆送完再附一張，並提醒 planner 不用自己發表情。挑哪張不經過 LLM：用 MaiBot 的 embedding 模型算表情庫描述的向量（收過的 Discord 表情與貼圖會自動換回原生的），照「情緒＋相關文字」排除這個聊天最近 10 次用過的，再從最接近的前 8 名照相似度加權抽一張；向量失敗時改用 MaiBot 照情緒標籤挑。請求失敗的那輪 planner 照舊自己決定。「DC生活 → 決策模型」頁面能看每天的判斷與送出、近 14 天走勢和最近紀錄，也能測試連線、清除統計；紀錄存在插件資料目錄的 `decision_stats.json`，向量快取在 `emote_vectors.json`。
- **输出转繁体**：用 OpenCC 把发到 Discord 的文字（含语音播报、修改消息）转成繁体，`s2twp` 会一起换成台湾用语；网址不转，MaiBot 自己的聊天记录不变。需要 `pip install opencc`。

---

## 日常功能

设置页「日常」标签里有三项，共用同一个后台排程：

- **定时提醒**：原 [maibot-reminder-plugin](https://github.com/banana2556/maibot-reminder-plugin) 并入，工具（`reminder_set` / `reminder_followup` / `reminder_list` / `reminder_cancel`）、`/reminders` 指令和 WebUI 页面都保持不变。第一次启动会把旧插件的 `reminders.json` 复制过来，**请停用旧的定时提醒插件**，否则提醒会重复触发。
- **日常生活**：每天在设定时间生成麦麦当天每小时在做什么，每隔几小时（默认 3）按最近的 Discord 聊天调整之后的时段（「按聊天调整时程」可关）；当前状态会加进对话上下文，并同步成 Discord 状态文字与在线状态。开启「睡觉时不回复」后，时程上在睡觉的时段只静默接收消息、不回复也不开话题（群聊直接把回复频率调成 0，私聊每轮只留 stop），提醒到点会叫醒她 30 分钟；被 @ 或收到私讯时按「被 @ 吵醒的机率」（默认 0.3）醒来，醒着的时候对话里会知道自己刚被吵醒。
- **主动话题**：按 cron 排程，根据群的长期记忆和最近聊天挑几个可能有兴趣的话题，唤醒麦麦自己找相关新闻或影片分享到群里。插件不替她搜索、也不指定工具，用哪个搜索或浏览工具、发不发都由她决定，所以 MaiBot 里要有可用的联网搜索。

开关、模型、生成时间这类核心设置在插件设置页；生活设定、主动话题的频道与排程，以及语音的频道模式、自动加入的频道、空闲退出与 VAD，改到顶部「DC生活」工作区的页面设置（存在插件数据目录的 `daily_settings.json`，第一次启动会从原配置搬过去），页面上还有立即重新生成时程、叫醒、立刻开一次话题、离开语音频道的按钮。

日常生活与主动话题会调用 LLM，默认关闭。

## Todo List

- [ ] 增添commands统一api接口
- [ ] 由语音聊天推广到视频识别->直播?
- [ ] 欢迎提[issues](https://github.com/litroenade/MaiBot-Discord-Adapter/issues)进行补充

---

## 排障建议

由于国内Discord平台被墙，如果长时间无法连接Discord平台请尝试开启tun模式(同时推荐代理内核使用singbox)

---
## 致谢

- [@UnCLAS-Prommer](https://github.com/UnCLAS-Prommer) - MaiBot-napcat-adapter 代码参考
---

## 开源协议

本项目采用 [GPL-v3.0](LICENSE) 协议开源
