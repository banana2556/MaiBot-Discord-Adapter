# MaiBot Discord Adapter

MaiBot 的 Discord 插件，跑在 MaiBot 的插件 Runner 裡（`maibot_sdk`）。工作分支 `rdev`，遠端 `banana2556/MaiBot-Discord-Adapter`。
正式機（Ubuntu）也會直接推 `rdev`：推之前先 `git fetch` 並 rebase，別覆蓋它的提交。

## 結構

- `plugin.py`：入口、訊息閘道器和大部分 Tool / Hook / `webui_*` API；按功能拆出去的 mixin 有定時提醒（`src/life/reminders.py` 的 `ReminderMixin`）和表情（`src/emote.py` 的 `EmoteMixin`），新功能照這個方式拆，不要再往 `plugin.py` 加。
- `config.py`：配置模型；`WEBUI_CONFIG_TABS` 決定設定頁分組，`PAGE_FIELDS` 是改到「DC生活」頁面設定的欄位（每頁一組，設定頁不再顯示，整節都搬走的設定節會整個隱藏）。
- `webui.json`：「DC生活」工作區頁面（日常生活、主動話題、語音、定時提醒、決策模型）。
- `src/recv_handler/`：Discord → MaiBot；`src/send_handler/`：MaiBot → Discord（含 OpenCC 轉繁體）。
- `src/voice/`：語音頻道、VAD、STT/TTS 服務商；`src/life/`：日常生活、主動話題、共用的狀態讀寫；`src/decision.py`：決策模型與統計。
- `src/emote.py`（`EmoteMixin`）：表情情境判斷、只按表情／只發圖、回覆後附圖、`discord_emote` 工具與決策模型頁 API；`src/emote_library.py`：MaiBot 表情庫的向量檢索。
- `../MaiBot`：MaiBot 本體，只讀參考（hook 載荷、capability 實作、WebUI 頁面 schema）。

## 測試

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt "maibot-plugin-sdk==2.21.0" "maim-message==0.6.8" numpy pytest ruff
.venv/Scripts/python -m pytest -q tests
.venv/Scripts/ruff check .
```

- CI（`.github/workflows/tests.yml`）在 `rdev` / `dev` / `master` 和 PR 上跑同樣的 ruff 與 pytest（Python 3.12），兩個都要保持全綠。
- 插件用相對匯入，測試一律經 `tests/test_typing_indicator.py` 的 `_load_adapter_module` / `_load_plugin_class` 載入，不要直接 import。
- 假的插件上下文參考 `tests/test_daily_features.py` 的 `FakeCtx`。

## 約定

- 語言：一律用繁體中文，包括程式碼註解、docstring、日誌、設定頁說明、給模型看的提示詞、「DC生活」頁面和 commit 訊息。現有的簡體是舊的，改到哪段就順手換成繁體，不用專門整批轉。用 OpenCC 轉時檢查用語：MaiBot 說「插件」不是「外掛」，「语音频道」別被轉成「語音訊道」。
- 新呼叫 Host 能力（`ctx.xxx`、`call_capability`）要加進 `_manifest.json` 的 `capabilities`，有測試檢查。新 Python 依賴同時加進 `requirements.txt`、`pyproject.toml`、`_manifest.json`。
- SDK 會把部分 capability 的成功結果拆開（`maibot_sdk.context._CAPABILITY_RESULT_KEYS`）：例如 `message.build_readable` 直接回文字、`chat.get_all_streams` 直接回清單，失敗或舊版 SDK 才回 `{"success": ..., ...}`。處理結果要兩種都接受，測試的假物件也要照拆開後的樣子回傳，不然測試會蓋掉真正的錯。
- 讀設定一律用 `self._load_settings()`，它會套上頁面設定（插件資料目錄的 `daily_settings.json`）；直接讀 `self.config` 在插件設定頁存檔後會拿到沒套頁面值的設定。
- 頁面改設定：`_setting_rows` 產生「目前的值」表格，`_save_page_fields` 只存有填的欄位並經過設定模型檢查。宣告式頁面的輸入框不能預填目前的值，所以一律是「上面看目前的值、下面只填要改的」。
- 狀態檔案放 `ctx.paths.data_dir`，讀寫用 `src/life/common.py` 的 `load_state` / `save_state`。
- 存任何插件設定都會觸發 `on_config_update`，整個 Discord 客戶端重建；長期存在的物件只存 ID，不要存 discord.py 的頻道、使用者、訊息物件。
- Hook 在 MaiBot 每輪 planner / replyer 的熱路徑上：blocking hook 要設 `timeout_ms`，出錯時放行（`{"action": "continue"}`）。
- 改 `webui.json` 用腳本（`json.dumps(..., ensure_ascii=False, indent=2)` 會保持原格式），改完用 MaiBot 本體驗證：
  `cd ../MaiBot && .venv/Scripts/python -c "from src.plugin_runtime.webui_schema import load_webui_extension as f; print([p.id for p in f(r'../MaiBot-Discord-Adapter').pages])"`
- 正式機的 MaiBot / SDK 比本地舊；用到較新的 Host 功能（新 capability、新頁面元件、hook 載荷欄位）前先確認正式機有。
- 刻意的簡化用 `# ponytail:` 註解寫明上限和升級方式。
- Commit 訊息用繁體中文 conventional commits，例如 `fix(send): …`、`feat(webui): …`。
