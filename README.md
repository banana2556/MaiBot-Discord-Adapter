<div align="center">

# MaiBot Discord Adapter

<p>
  <a href="https://github.com/banana2556/MaiBot-Discord-Adapter/actions/workflows/tests.yml">
    <img src="https://github.com/banana2556/MaiBot-Discord-Adapter/actions/workflows/tests.yml/badge.svg?branch=rdev" alt="Tests">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License">
  </a>
</p>

讓 [MaiBot](https://github.com/Mai-with-u/MaiBot)（麥麥）住進 Discord 的插件。

不只是收發訊息的橋接器：她看得懂圖片、影片、語音訊息和連結預覽，會按表情、發貼圖、改自己說錯的話，
有自己的作息、會主動開話題、記得幫人提醒，也能進語音頻道聊天。

</div>

---

## 功能

### 聊天

- **收發**：伺服器頻道、私訊、子區（Thread）、引用回覆、提及、轉發的訊息；送出的訊息 ID 會回寫給 MaiBot，之後引用、按表情、修改都對得上。
- **回覆舊訊息**：MaiBot 的上下文只標出回覆了哪則、不附內容。被回覆的那則已經不在她看得到的聊天紀錄裡時，插件會把那則和它之前的幾則對話補給 planner 和 replyer，她才知道對方在回什麼。
- **有人找她時先回應**：MaiBot 被 @ 或被回覆時會強制開一輪 planner，但沒告訴 planner 是為誰開的，她常常接著聊最新的話題、把找她的人晾著。插件會把她上次說話之後 @ 她、回覆她或喊她名字的訊息列給 planner，要她先回應這幾則。
- **看得懂的內容**：都轉成 MaiBot 能理解的形式再交給她。
  - 圖片、貼圖、伺服器自訂表情（收過的會存進 MaiBot 表情庫）；貼圖附上名字和說明，Lottie 動畫貼圖沒有圖片，只給名字和說明
  - 影片：按固定秒數截幾格拼成一張圖給識圖，直接讀 Discord CDN 的片段，不整個下載
  - 動圖（GIF、動態 PNG／WebP）：平均取 4 格拼成一張靜態圖再交給 MaiBot，圖片記憶只收靜態圖
  - 語音訊息：轉成 wav 交給 MaiBot 的語音轉文字
  - 連結：等 Discord 展開預覽；圖片與動圖（Klipy、Tenor、Giphy）當圖片，網頁卡片取標題、描述和縮圖
  - 修改和刪除訊息；別人按的表情不送進 MaiBot（不是對話內容），只給決策模型當「已經有人按了什麼」的參考
- **她能做的事**（MaiBot 的工具）：
  - `discord_emote`：在訊息上按表情、單發一個伺服器表情，或發一張貼圖；只挑這個伺服器能直接發的貼圖和自訂表情，不會發成圖片附件
  - `discord_unreact`：收回自己按的表情
  - `discord_message_edit` / `discord_message_delete`：修改、刪除自己發過的訊息
- **輸入中提示**：她在想怎麼回的時候顯示「正在輸入…」。
- **輸出轉繁體**：用 OpenCC 把發到 Discord 的文字轉成繁體（`s2twp` 會一起換成台灣用語），網址不轉，MaiBot 自己的聊天紀錄不變。
- **名單過濾**：伺服器、頻道、子區、使用者各自可設白名單或黑名單；子區可以沿用所屬頻道的權限和記憶。

### 表情與決策模型

可以接一個只回機率的「決策模型」（[vLLM-SR Decision](https://app.vllm-sr.ai) 有免費額度，或 [Jev](https://api.typesafe.ai)，兩家都是 `POST /v1/systemone`，填位址、Key 和模型名就能切換），讓她在群聊裡像人一樣表態：

- 有新訊息、第一次進 planner 時，用 planner 這輪看到的同一份聊天判斷：這輪該「只回文字」「文字加一張圖」「只按表情」還是「只發圖」，最值得表態的是哪一則（不一定是最新一則），要表達什麼情緒。
- 只按表情／只發圖由插件直接替她做；文字加圖會等她回完再補一張。結果只用一句提示告訴 planner，不拿掉她的工具，不打亂 MaiBot 原本的回覆流程。
- 挑哪張圖不經過 LLM：只從這個伺服器能直接發的貼圖和自訂表情挑，用 MaiBot 的 embedding 模型比對「名字：描述」（描述來自 MaiBot 表情庫或貼圖自己的說明），避開這個聊天最近用過的，再從最接近的幾張裡加權抽一張，減少重複。
- 「有沒有人在叫她」和 MaiBot 的被提及判斷一致：@ 她、回覆她、私訊，或提到 `bot.nickname` / `bot.alias_names` 裡的名字。
- 送幾則聊天、每一題的提示詞、情境說明和情緒選項，都能在「DC生活 → 決策模型」頁面調整，也能一鍵恢復預設。

沒開決策模型時，她照樣可以自己用 `discord_emote`。

### 日常

- **日常生活**：每天在設定時間生成她當天每小時在做什麼，每隔幾小時按最近的聊天調整之後的時段（可關）。目前狀態會加進對話上下文，並同步成 Discord 的狀態文字與上線狀態。
  - 開啟「睡覺時不回覆」後，睡覺時段只靜默接收；被 @ 或收到私訊時有機率被吵醒，醒來後知道自己剛被吵醒。
- **主動話題**：按 cron 排程，參考群的長期記憶和最近聊天挑幾個方向，喚醒她去找最近的熱門內容分享（預設會到 Bilibili、X、Threads 看大家在討論什麼）。插件不替她搜尋，所以 MaiBot 裡要有可用的聯網搜尋工具；兩段提示詞都能在頁面調整。她真的把連結發到群裡才算分享；寫回覆時漏了連結會自動補發，頁面紀錄會顯示每次是「已分享」還是「沒發出去」。
- **定時提醒**：原 [maibot-reminder-plugin](https://github.com/banana2556/maibot-reminder-plugin) 併入。
  - 工具：`reminder_set`、`reminder_followup`（順手記下 24 小時內的安排，到時關心一下）、`reminder_list`、`reminder_cancel`
  - 指令：`/reminders`、`/reminder_cancel <id>`
  - 第一次啟動會把舊插件的 `reminders.json` 複製過來，**請停用舊的定時提醒插件**，不然提醒會重複觸發。

日常生活與主動話題會呼叫 LLM，預設關閉。

### 語音頻道

- 固定常駐一個頻道，或在允許的頻道之間跟著發話者走（`discord_voice_join` / `discord_voice_leave`），空閒一段時間自動離開。
- 用 VAD 偵測說話段落，語音轉文字後交給 MaiBot，回覆再用 TTS 唸出來；除錯時可以讓她同時把文字發在語音頻道的文字區。
- 語音轉文字：`siliconflow_sensevoice`、`aliyun`、`tencent`、`gemini`（Gemini Live API）
- 文字轉語音：`siliconflow`、`gptsovits`、`minimax`、`fishaudio`

細節見 [語音設定指南](./docs/voice_config_guide.md)。

### 給其他插件用的 API

- `adapter.discord.channel.send_message`：發訊息到指定的 Discord 頻道
- `adapter.discord.presence.set`：設定 Bot 的狀態文字與上線狀態

---

## 安裝

需要：

- MaiBot 1.x（插件 SDK 2.x）
- Python 依賴見 [`requirements.txt`](./requirements.txt)
- [FFmpeg](https://ffmpeg.org/)：影片截圖、語音訊息、語音頻道播放都要用到
- Discord Bot：在 Developer Portal 開啟 **Message Content Intent**

步驟：

1. 把這個倉庫放到 `MaiBot/plugins/maibot-discord-adapter`，安裝依賴：
   ```bash
   pip install -r requirements.txt
   ```
2. 啟動 MaiBot，在 WebUI 的插件設定裡啟用插件並填 `connection.token`。
3. 把 Bot 邀請進伺服器。建立 Bot、拿 Token、設定權限的圖文步驟見 [安裝設定指南](./docs/setup_guide.md)。
4. 要用語音的話，同時開啟 `voice.enabled` 和 `connection.intent_voice_states`，Bot 在語音頻道要有「檢視頻道」「連線」「說話」權限。

---

## 設定

插件設定頁只放連線和金鑰這類設定，分成三個標籤：

| 標籤 | 內容 |
| --- | --- |
| 基礎設定 | 開關、Token、Intents、重連與健康檢查、平台名稱 |
| 聊天與過濾 | 名單過濾、子區、輸入中提示、表情機率、影片截圖間隔、輸出轉繁體、決策模型的 API Key |
| 語音 | 語音頻道開關、STT／TTS 服務商與各家金鑰 |

各功能的設定在 MaiBot 頂部「**DC生活**」工作區，每頁都能看目前的值、直接修改（只填要改的項目），大多改了不用重啟 Discord 連線：

- **日常生活**：開關、生活設定、生成時間、調整間隔、睡覺時不回覆等；今天的時程、立即重新生成、叫醒她
- **主動話題**：開關、頻道、排程、新聞／影片、參考時間、挑話題與分享的提示詞；分享紀錄、立刻開一次話題
- **語音**：頻道模式、自動加入的頻道、空閒退出、VAD；讓她離開語音頻道（存檔會重新連線）
- **定時提醒**：開關、上限、順手記下；所有聊天待觸發的提醒，可直接取消
- **決策模型**：開關、位址、模型、門檻、送出則數、提示詞、情境說明、情緒選項；每天的判斷與送出統計、近 14 天走勢、最近 10 筆紀錄、測試連線

插件的狀態都存在 MaiBot 給插件的資料目錄：

| 檔案 | 用途 |
| --- | --- |
| `daily_settings.json` | 「DC生活」頁面上的設定 |
| `life_schedule.json` | 今天的時程 |
| `topics.json` | 主動話題的分享紀錄 |
| `reminders.json` | 定時提醒 |
| `native_emoji.json` | 表情庫圖片對應的 Discord 原生表情／貼圖 |
| `emote_vectors.json` | 貼圖與表情「名字：描述」的向量快取 |
| `decision_stats.json` | 決策模型統計 |

---

## 開發

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt "maibot-plugin-sdk==2.21.0" "maim-message==0.6.8" numpy pytest ruff
.venv/Scripts/python -m pytest -q tests
.venv/Scripts/ruff check .
```

CI 在每次推送和 PR 上跑同樣的 ruff 與 pytest。架構、慣例和踩過的坑寫在 [`CLAUDE.md`](./CLAUDE.md)。

問題與建議請開 [issue](https://github.com/banana2556/MaiBot-Discord-Adapter/issues)。

---

## 致謝

- 這個專案最初分支自 [@2829798842](https://github.com/2829798842)（litroenade）的 MaiBot-Discord-Adapter，之後大幅改寫。
- [@UnCLAS-Prommer](https://github.com/UnCLAS-Prommer) 的 MaiBot-napcat-adapter：早期程式碼參考。

## 授權

[GPL-3.0](LICENSE)
