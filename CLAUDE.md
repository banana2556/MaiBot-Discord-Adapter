# MaiBot Discord Adapter

MaiBot 的 Discord 插件，跑在 MaiBot 的插件 Runner 裡（`maibot_sdk`）。工作分支 `rdev`，遠端 `banana2556/MaiBot-Discord-Adapter`。
正式機（Ubuntu）也會直接推 `rdev`：推之前先 `git fetch` 並 rebase，別覆蓋它的提交。

## 結構

- `plugin.py`：入口、訊息閘道器和大部分 Tool / Hook / `webui_*` API；按功能拆出去的 mixin 有定時提醒（`src/life/reminders.py` 的 `ReminderMixin`）、話題分析與表情工具（`src/emote.py` 的 `EmoteMixin`：每輪分話題串、選焦點）、焦點過濾（`src/quote_context.py` 的 `QuoteContextMixin`：planner、表達方式選擇和 replyer 只看焦點那串，加上回覆標籤、有人在找你）和聯想（`src/recall.py` 的 `RecallMixin`：理解焦點那串，參與者的人物畫像、這串的黑話、相關的群記憶，看不懂時提醒她先查；話題串在 `src/topics.py`），新功能照這個方式拆，不要再往 `plugin.py` 加。
- `config.py`：配置模型；`WEBUI_CONFIG_TABS` 決定設定頁分組，`PAGE_FIELDS` 是改到「DC生活」頁面設定的欄位（每頁一組，設定頁不再顯示，整節都搬走的設定節會整個隱藏）。
- `webui.json`：「DC生活」工作區頁面（日常生活、主動話題、語音、定時提醒、話題串、話題分析設定）。「話題串」是 custom 頁面（`webui/threads.html`，沙盒 iframe，只能用 `window.maibot.query` 呼叫宣告過的 API，主題色用 `--maibot-*` CSS 變數），資料來自 `webui_threads`。
- `src/recv_handler/`：Discord → MaiBot；`src/send_handler/`：MaiBot → Discord（含 OpenCC 轉繁體）。
- `src/voice/`：語音頻道、VAD、STT/TTS 服務商；`src/life/`：日常生活、主動話題、共用的狀態讀寫；`src/decision.py`：決策模型與統計。
- `src/emote.py`（`EmoteMixin`）：話題分析的入口（分話題、選焦點、跑聯想）、`discord_emote` 工具與話題分析頁 API；要不要說話、按不按表情由 planner 自己決定。`src/emote_library.py`：MaiBot 表情庫的向量檢索。
- `../MaiBot`：MaiBot 本體，用 fork `banana2556/MaiBot` 的 `custom` 分支（遠端 `fork`）：上游正式版加上我們的修改，正式機也跑這個分支。
  平常只讀參考（hook 載荷、capability 實作、WebUI 頁面 schema）；要改主程式先請求許可，改了提交到 `custom`。
  目前的修改：記憶檢索的實體查詢不用刪除索引、replyer 照 `reply_tool_args["_keep_message_ids"]` 過濾聊天紀錄（焦點話題串用）、
  給插件的能力 `person.get_profile`（人物畫像）和 `jargon.match`（給定文字命中的黑話）。

## 測試

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt "maibot-plugin-sdk==2.23.0" "maim-message==0.6.8" numpy pytest ruff
.venv/Scripts/python -m pytest -q tests
.venv/Scripts/ruff check .
```

- CI（`.github/workflows/tests.yml`）在 `rdev` / `dev` / `master` 和 PR 上跑同樣的 ruff 與 pytest（Python 3.12），兩個都要保持全綠。
- 插件用相對匯入，測試一律經 `tests/test_typing_indicator.py` 的 `_load_adapter_module` / `_load_plugin_class` 載入，不要直接 import。
- 假的插件上下文參考 `tests/test_daily_features.py` 的 `FakeCtx`。

## 約定

- 語言：一律用繁體中文，包括程式碼註解、docstring、日誌、設定頁說明、給模型看的提示詞、「DC生活」頁面和 commit 訊息。現有的簡體是舊的，改到哪段就順手換成繁體，不用專門整批轉。用 OpenCC 轉時檢查用語：MaiBot 說「插件」不是「外掛」，「语音频道」別被轉成「語音訊道」。
- 新呼叫 Host 能力（`ctx.xxx`、`call_capability`）要加進 `_manifest.json` 的 `capabilities`，有測試檢查。新 Python 依賴同時加進 `requirements.txt`、`pyproject.toml`、`_manifest.json`。
- SDK 會把部分 capability 的成功結果拆開（`maibot_sdk.context._CAPABILITY_RESULT_KEYS`）：例如 `message.build_readable` 直接回文字、`chat.get_all_streams` 直接回清單，失敗時才回原樣的 `{"success": False, "error": ...}`。處理結果要兩種都接受，測試的假物件也要照拆開後的樣子回傳，不然測試會蓋掉真正的錯。
- 讀設定一律用 `self._load_settings()`，它會套上頁面設定（插件資料目錄的 `daily_settings.json`）；直接讀 `self.config` 在插件設定頁存檔後會拿到沒套頁面值的設定。
- 頁面改設定：`_setting_rows` 產生「目前的值」表格，`_save_page_fields` 只存有填的欄位並經過設定模型檢查。宣告式頁面的輸入框不能預填目前的值，所以一律是「上面看目前的值、下面只填要改的」。
- 狀態檔案放 `ctx.paths.data_dir`，讀寫用 `src/life/common.py` 的 `load_state` / `save_state`。
- 存任何插件設定都會觸發 `on_config_update`，整個 Discord 客戶端重建；長期存在的物件只存 ID，不要存 discord.py 的頻道、使用者、訊息物件。
- Hook 在 MaiBot 每輪 planner / replyer 的熱路徑上：blocking hook 要設 `timeout_ms`，出錯時放行（`{"action": "continue"}`）。
- 改 `webui.json` 用腳本（`json.dumps(..., ensure_ascii=False, indent=2)` 會保持原格式），改完用 MaiBot 本體驗證：
  `cd ../MaiBot && .venv/Scripts/python -c "from src.plugin_runtime.webui_schema import load_webui_extension as f; print([p.id for p in f(r'../MaiBot-Discord-Adapter').pages])"`
- 正式機一律視為最新版 MaiBot / SDK（和本地 `../MaiBot` 一樣），不要為舊版做相容或保險；版本落後由正式機升級解決。
- 刻意的簡化用 `# ponytail:` 註解寫明上限和升級方式。
- Commit 訊息用繁體中文 conventional commits，例如 `fix(send): …`、`feat(webui): …`。
