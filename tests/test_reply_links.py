"""頁面設定：拿掉的功能留下的舊設定會被清掉。"""

import json

from test_daily_features import _plugin


def test_old_page_settings_from_removed_features_are_dropped(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path)
    path = tmp_path / "daily_settings.json"
    # 上一版「話題切分」頁存的設定、已經不歸頁面管的欄位，和拿掉的表情情境設定
    path.write_text(
        json.dumps({"threads": {"enabled": True, "ledger_every": 15}, "decision": {"enabled": True, "gone": 1, "emote_threshold": 0.4}}),
        encoding="utf-8",
    )
    plugin._load_page_settings(path)
    settings = plugin._load_settings()
    assert settings.decision.enabled is True and not hasattr(settings.decision, "emote_threshold")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "threads" not in saved and "gone" not in saved["decision"] and "emote_threshold" not in saved["decision"]
