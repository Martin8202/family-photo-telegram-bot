"""
維運設定的測試：log 輪替保留期、雜訊壓制、long polling 等待時間。

這些都是「設定對不對」的測試，看起來瑣碎，但它們防的是**無聲的退化**：
改回 `FileHandler` 不會有任何錯誤訊息，log 只是又開始無限長大；
把 `timeout` 拿掉也不會壞，只是閒置時的請求量默默變回 5 倍。
沒有測試的話，這兩件事都要等到幾個月後才會被發現。
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

import bot


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    """
    讓 setup_logging() 寫到 tmp_path，並在測試結束後把 root logger 完整還原。

    不還原的話，後續測試的 log 會被導去已刪除的暫存目錄——這種汙染很難查。
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_noisy = {name: logging.getLogger(name).level for name in bot.NOISY_LOGGERS}

    for handler in saved_handlers:
        root.removeHandler(handler)
    monkeypatch.setattr(bot, "LOG_DIR", tmp_path)

    yield tmp_path

    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_noisy.items():
        logging.getLogger(name).setLevel(level)


def _file_handler(root):
    for handler in root.handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            return handler
    return None


# ── log 輪替與保留期 ─────────────────────────────────────

def test_log_rotates_daily_instead_of_growing_forever(isolated_logging):
    """原本用 FileHandler，5 天長到 8.8 MB 且永不縮小。"""
    bot.setup_logging()
    handler = _file_handler(logging.getLogger())

    assert handler is not None, "找不到輪替用的 file handler——是不是改回 FileHandler 了？"
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == bot.LOG_RETENTION_DAYS


def test_retention_is_about_one_week(isolated_logging):
    assert bot.LOG_RETENTION_DAYS == 7


def test_backups_older_than_retention_are_deleted(tmp_path):
    """
    行為測試：鋪出 10 天份的舊 log，確認超出保留期的最舊 3 支會被挑出來刪。

    只斷言「設定成 7」還不夠——要確認這個設定真的會讓舊檔消失，
    否則 log 目錄一樣會無限長大，只是換成很多支小檔。
    """
    handler = TimedRotatingFileHandler(
        tmp_path / "photo-bot.log",
        when="midnight",
        backupCount=bot.LOG_RETENTION_DAYS,
        encoding="utf-8",
    )
    try:
        for day in range(1, 11):
            (tmp_path / f"photo-bot.log.2026-07-{day:02d}").write_text("x", encoding="utf-8")
        doomed = [Path(p).name for p in handler.getFilesToDelete()]
    finally:
        handler.close()

    assert doomed == [
        "photo-bot.log.2026-07-01",
        "photo-bot.log.2026-07-02",
        "photo-bot.log.2026-07-03",
    ], "應該只刪最舊的 3 支、留下最近 7 支"


# ── 雜訊壓制 ─────────────────────────────────────────────

def test_routine_third_party_chatter_is_quieted(isolated_logging):
    """httpx 與 apscheduler 佔了整份 log 的 90%。"""
    bot.setup_logging()
    for name in bot.NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_quieting_hides_routine_lines_but_keeps_real_problems(isolated_logging):
    """
    關鍵區別：壓掉的是例行 INFO，**不是**錯誤。

    這個測試存在的理由——如果哪天有人為了「更安靜」把等級調到 ERROR 甚至
    CRITICAL，網路異常的線索就會消失，而這正是當初查 502 時唯一的憑據。
    """
    log_dir = isolated_logging
    bot.setup_logging()

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.info('HTTP Request: POST .../getUpdates "HTTP/1.1 200 OK"')
    httpx_logger.warning("連線異常：Bad Gateway")

    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (log_dir / "photo-bot.log").read_text(encoding="utf-8")

    assert "200 OK" not in content, "例行的成功輪詢不該再佔版面"
    assert "連線異常：Bad Gateway" in content, "真正的問題必須留下來"


def test_own_logs_are_not_affected(isolated_logging):
    """壓的是第三方套件，本程式自己的 INFO 要照常留著。"""
    log_dir = isolated_logging
    bot.setup_logging()

    logging.getLogger("photo-bot.upload").info("複製完成：3 張")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "複製完成：3 張" in (log_dir / "photo-bot.log").read_text(encoding="utf-8")


# ── long polling 等待時間 ────────────────────────────────

def test_poll_timeout_matches_telegram_server_cap():
    """
    50 秒是**實測出來的天花板**，不是隨手挑的數字。

    Telegram 伺服器不論你要求多久都在約 50.2 秒關閉連線（有人設 300 秒、
    量了 36 次請求，全都落在 50.18~50.21 秒）。設得比這更大沒有任何效果。
    """
    assert bot.POLL_TIMEOUT_SEC == 50


def test_main_actually_passes_the_timeout_to_polling(monkeypatch):
    """
    常數定義了但沒接上去，是這類改動最典型的失敗方式——
    程式照跑、沒有錯誤，只是設定完全沒生效。
    """
    captured = {}

    class DummyApp:
        def run_polling(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(bot, "setup_logging", lambda: None)
    monkeypatch.setattr(bot, "build_application", lambda: DummyApp())

    bot.main()

    assert captured["timeout"] == bot.POLL_TIMEOUT_SEC
    assert captured["timeout"] > 10, "10 是 PTB 預設值，代表設定沒生效"


def test_read_timeout_leaves_room_for_the_server_response(monkeypatch):
    """
    PTB 會把 HTTP read timeout 設為 `基礎值 + polling timeout`。
    若這個和小於伺服器實際回應的 ~50.2 秒，每一次輪詢都會逾時——
    這是拉長等待時間時最容易踩到的坑，所以留一個測試守著。
    """
    from telegram.request import HTTPXRequest

    base = HTTPXRequest().read_timeout or 0
    assert base + bot.POLL_TIMEOUT_SEC > 50.2
