from datetime import datetime, timedelta
from pathlib import Path

from state import (
    STAGE_AWAITING_DESTINATION,
    STAGE_AWAITING_FOLDER,
    STAGE_DEBOUNCE,
    STAGE_PROCESSING,
    STAGE_RECEIVING_PHOTOS,
    ReceivedFile,
    SessionManager,
    chunk_files,
    group_by_media_group,
    progress_bar,
    recent_folder_icon,
    should_update_counter,
)


# ── session 逾時判定（§6.4、測項 E10-E11）────────────

def test_idle_timeout_only_counts_in_receiving_photos_stage():
    mgr = SessionManager()
    s = mgr.start(1, "秀琴", now=datetime(2026, 1, 1, 12, 0, 0))
    s.enter_stage(STAGE_RECEIVING_PHOTOS, now=datetime(2026, 1, 1, 12, 0, 0))

    almost_timeout = datetime(2026, 1, 1, 12, 9, 59)
    assert not s.is_idle_timed_out(10, now=almost_timeout)

    past_timeout = datetime(2026, 1, 1, 12, 10, 1)
    assert s.is_idle_timed_out(10, now=past_timeout)


def test_processing_stage_never_times_out_even_after_long_time():
    """處理中（下載/複製/重試/進度回報）不計入閒置，即使耗時數分鐘（§6.4、測項 E11）。"""
    mgr = SessionManager()
    s = mgr.start(1, "秀琴", now=datetime(2026, 1, 1, 12, 0, 0))
    s.enter_stage(STAGE_PROCESSING, now=datetime(2026, 1, 1, 12, 0, 0))
    much_later = datetime(2026, 1, 1, 13, 0, 0)
    assert not s.is_idle_timed_out(10, now=much_later)


def test_awaiting_folder_or_destination_never_times_out():
    """等待使用者選資料夾/目的地不計入閒置（§6.4）。"""
    mgr = SessionManager()
    s = mgr.start(1, "秀琴", now=datetime(2026, 1, 1, 12, 0, 0))
    for stage in (STAGE_AWAITING_FOLDER, STAGE_AWAITING_DESTINATION):
        s.enter_stage(stage, now=datetime(2026, 1, 1, 12, 0, 0))
        much_later = datetime(2026, 1, 1, 12, 30, 0)
        assert not s.is_idle_timed_out(10, now=much_later)


def test_debounce_stage_never_times_out():
    mgr = SessionManager()
    s = mgr.start(1, "秀琴", now=datetime(2026, 1, 1, 12, 0, 0))
    s.enter_stage(STAGE_DEBOUNCE, now=datetime(2026, 1, 1, 12, 0, 0))
    later = datetime(2026, 1, 1, 12, 30, 0)
    assert not s.is_idle_timed_out(10, now=later)


def test_activity_resets_idle_clock():
    mgr = SessionManager()
    s = mgr.start(1, "秀琴", now=datetime(2026, 1, 1, 12, 0, 0))
    s.enter_stage(STAGE_RECEIVING_PHOTOS, now=datetime(2026, 1, 1, 12, 0, 0))
    s.touch(now=datetime(2026, 1, 1, 12, 9, 0))  # 例如收到一張照片
    still_within = datetime(2026, 1, 1, 12, 15, 0)  # 距上次活動只過 6 分鐘
    assert not s.is_idle_timed_out(10, now=still_within)


# ── session manager 基本行為（§6.3 重複點擊）─────────

def test_session_manager_start_get_clear():
    mgr = SessionManager()
    assert not mgr.has_active(1)
    mgr.start(1, "秀琴")
    assert mgr.has_active(1)
    mgr.clear(1)
    assert not mgr.has_active(1)


def test_last_batch_tracked_separately_from_active_session():
    from state import CompletedBatch
    mgr = SessionManager()
    mgr.start(1, "秀琴")
    mgr.clear(1)
    assert mgr.get_last_batch(1) is None
    batch = CompletedBatch(telegram_id=1, folder="阿嬤生日", destination_label="家裡硬碟",
                            files=[], written_paths={}, completed_at=datetime.now())
    mgr.set_last_batch(batch)
    assert mgr.get_last_batch(1) is batch


# ── 批次切分（BATCH_SIZE，§6.3、C5）───────────────────

def test_chunk_files_splits_by_batch_size():
    files = list(range(45))
    chunks = chunk_files(files, 20)
    assert [len(c) for c in chunks] == [20, 20, 5]


def test_chunk_files_empty_list():
    assert chunk_files([], 20) == []


def test_chunk_files_exact_multiple():
    files = list(range(40))
    chunks = chunk_files(files, 20)
    assert [len(c) for c in chunks] == [20, 20]


# ── media group 聚合（相簿，§6.3、C7）─────────────────

def _rf(file_id, media_group_id=None):
    return ReceivedFile(
        temp_path=Path(f"/tmp/{file_id}.jpg"), filename=f"{file_id}.jpg", file_id=file_id,
        media_group_id=media_group_id, received_at=datetime.now(), is_original_quality=False,
    )


def test_group_by_media_group_aggregates_album():
    files = [_rf("a", "g1"), _rf("b", "g1"), _rf("c", None), _rf("d", "g1")]
    groups = group_by_media_group(files)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 3]
    total = sum(len(g) for g in groups)
    assert total == len(files)  # 計數不重複、不遺漏（C7）


def test_group_by_media_group_all_singles():
    files = [_rf("a"), _rf("b"), _rf("c")]
    groups = group_by_media_group(files)
    assert len(groups) == 3


# ── 收件計數節流（§6.3.1、C1）──────────────────────────

def test_should_update_counter_first_time_always_true():
    mgr = SessionManager()
    s = mgr.start(1, "秀琴")
    assert should_update_counter(s, datetime.now(), throttle_sec=5)


def test_should_update_counter_throttled_within_window():
    mgr = SessionManager()
    s = mgr.start(1, "秀琴")
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    s.counter_last_update = t0
    assert not should_update_counter(s, t0 + timedelta(seconds=2), throttle_sec=5)
    assert should_update_counter(s, t0 + timedelta(seconds=5), throttle_sec=5)


# ── 進度條 ───────────────────────────────────────────

def test_progress_bar_format():
    assert progress_bar(40, 100, width=10) == "▓▓▓▓░░░░░░ 40%（40/100 張）"


def test_progress_bar_complete():
    assert progress_bar(100, 100, width=10) == "▓▓▓▓▓▓▓▓▓▓ 100%（100/100 張）"


def test_progress_bar_zero_total_no_crash():
    progress_bar(0, 0)  # 不應拋除以零錯誤


# ── 近期資料夾圖示（§6.2）────────────────────────────

def test_recent_folder_icon_mapping():
    assert recent_folder_icon("家裡硬碟") == "🏠"
    assert recent_folder_icon("OneDrive") == "☁️"
    assert recent_folder_icon("兩邊都存") == "📦"
