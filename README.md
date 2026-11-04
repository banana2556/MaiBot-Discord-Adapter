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
  - 图片链接与网页卡片的预览图（等 Discord 展开预览后当图片交给识图）
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

## 日常功能

设置页「日常」标签里有三项，共用同一个后台排程：

- **定时提醒**：原 [maibot-reminder-plugin](https://github.com/banana2556/maibot-reminder-plugin) 并入，工具（`reminder_set` / `reminder_followup` / `reminder_list` / `reminder_cancel`）、`/reminders` 指令和 WebUI 页面都保持不变。第一次启动会把旧插件的 `reminders.json` 复制过来，**请停用旧的定时提醒插件**，否则提醒会重复触发。
- **日常生活**：每天在设定时间生成麦麦当天每小时在做什么，每隔几小时（默认 3）按最近的 Discord 聊天调整之后的时段；当前状态会加进对话上下文，并同步成 Discord 状态文字与在线状态。开启「睡觉时不回复」后，时程上在睡觉的时段只静默接收消息、不回复也不开话题（群聊直接把回复频率调成 0，私聊每轮只留 stop），提醒到点会叫醒她 30 分钟。
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
