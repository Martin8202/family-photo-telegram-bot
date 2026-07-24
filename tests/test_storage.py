import time
from datetime import datetime
from pathlib import Path

import pytest

import storage


# ── 檔名 / EXIF（規格書 §10、測項 G1/G2/G3）──────────

def test_build_filename_falls_back_to_received_time_without_exif(tmp_path):
    received = datetime(2026, 7, 23, 21, 40, 5, 123456)
    name = storage.build_filename(received, ext=".jpg", source_path=None, use_exif=True)
    assert name == "20260723_214005_123456.jpg"


def test_build_filename_format():
    received = datetime(2008, 2, 27, 14, 30, 15, 654321)
    name = storage.build_filename(received, ext=".jpg", source_path=None, use_exif=False)
    assert name == "20080227_143015_654321.jpg"


def test_read_exif_datetime_missing_file_returns_none(tmp_path):
    # 不存在或非圖片檔：不可拋例外，一律回傳 None（回退接收時間，§G3）
    fake = tmp_path / "not_a_real_image.jpg"
    fake.write_bytes(b"not an image")
    assert storage.read_exif_datetime(fake) is None


def _make_photo_with_exif_original(img_path, dt_str: str):
    """
    產生一張把 DateTimeOriginal 存在「Exif 子標籤頁」的照片——這才是真實相機/手機
    的存法。若寫進根標籤頁（早期測試的錯誤寫法）會測不到實際的讀取路徑。
    """
    from PIL import Image

    img = Image.new("RGB", (4, 4), color="red")
    exif = img.getexif()
    sub = exif.get_ifd(storage.EXIF_SUB_IFD_TAG)
    sub[storage.TAG_DATETIME_ORIGINAL] = dt_str
    img.save(img_path, exif=exif)


def test_read_exif_datetime_from_sub_ifd(tmp_path):
    """回歸測試：DateTimeOriginal 存在子標籤頁時必須讀得到（先前的 bug 就是讀不到）。"""
    img_path = tmp_path / "camera.jpg"
    _make_photo_with_exif_original(img_path, "2008:02:27 14:30:15")
    dt = storage.read_exif_datetime(img_path)
    assert dt == datetime(2008, 2, 27, 14, 30, 15)


def test_build_filename_uses_exif_when_present(tmp_path):
    img_path = tmp_path / "photo.jpg"
    _make_photo_with_exif_original(img_path, "2008:02:27 14:30:15")

    received = datetime(2026, 7, 23, 10, 0, 0, 999999)
    name = storage.build_filename(received, ext=".jpg", source_path=img_path, use_exif=True)
    # 時間戳採 EXIF 拍攝時間，非今天；微秒仍採接收時間避免同批撞名
    assert name.startswith("20080227_143015_")
    assert name.endswith("999999.jpg")


# ── 撞名保護：絕不覆蓋（§10、測項 E8）────────────────

def test_unique_destination_no_collision(tmp_path):
    dest = storage.unique_destination(tmp_path, "20260723_214005_123456.jpg")
    assert dest == tmp_path / "20260723_214005_123456.jpg"


def test_unique_destination_appends_suffix_on_collision(tmp_path):
    existing = tmp_path / "a.jpg"
    existing.write_bytes(b"original")
    dest = storage.unique_destination(tmp_path, "a.jpg")
    assert dest == tmp_path / "a_(2).jpg"
    assert existing.read_bytes() == b"original"  # 原檔完全未被觸碰


def test_unique_destination_multiple_collisions(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"1")
    (tmp_path / "a_(2).jpg").write_bytes(b"2")
    dest = storage.unique_destination(tmp_path, "a.jpg")
    assert dest == tmp_path / "a_(3).jpg"


def test_sanitize_folder_name_strips_invalid_chars():
    assert storage.sanitize_folder_name('阿嬤/生日:测试*?"<>|') == "阿嬤生日测试"


# ── 複製：只複製、不覆蓋、成功回傳實際路徑 ────────────

def test_copy_file_creates_dest_dir_and_copies(tmp_path):
    src = tmp_path / "src.jpg"
    src.write_bytes(b"hello")
    dest_dir = tmp_path / "dest" / "資料夾"
    result_path = storage.copy_file(src, dest_dir, "out.jpg")
    assert result_path.exists()
    assert result_path.read_bytes() == b"hello"
    assert src.exists()  # 來源檔案不受影響（複製，非移動）


def test_copy_file_never_overwrites_same_name(tmp_path):
    src1 = tmp_path / "src1.jpg"
    src1.write_bytes(b"AAA")
    src2 = tmp_path / "src2.jpg"
    src2.write_bytes(b"BBB")
    dest_dir = tmp_path / "dest"

    p1 = storage.copy_file(src1, dest_dir, "same.jpg")
    p2 = storage.copy_file(src2, dest_dir, "same.jpg")

    assert p1 != p2
    assert p1.read_bytes() == b"AAA"
    assert p2.read_bytes() == b"BBB"


def test_copy_file_with_retry_success_first_try(tmp_path):
    src = tmp_path / "src.jpg"
    src.write_bytes(b"data")
    result = storage.copy_file_with_retry(src, tmp_path / "dest", "out.jpg", retry_times=3, retry_delays=[0, 0, 0])
    assert result.success
    assert result.attempts == 1
    assert result.dest_path.read_bytes() == b"data"


def test_copy_file_with_retry_all_fail_returns_error(tmp_path):
    missing_src = tmp_path / "does_not_exist.jpg"
    slept = []
    result = storage.copy_file_with_retry(
        missing_src, tmp_path / "dest", "out.jpg",
        retry_times=3, retry_delays=[0, 0, 0], sleep_fn=slept.append,
    )
    assert not result.success
    assert result.attempts == 3
    assert result.error is not None
    assert len(slept) == 2  # 重試間隔次數 = retry_times - 1


def test_copy_file_with_retry_succeeds_after_transient_failure(tmp_path):
    src = tmp_path / "src.jpg"
    src.write_bytes(b"data")
    dest_dir = tmp_path / "dest"

    calls = {"n": 0}
    original_copy = storage.copy_file

    def flaky_copy(s, d, f):
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("模擬網芳暫時性錯誤")
        return original_copy(s, d, f)

    storage.copy_file = flaky_copy
    try:
        result = storage.copy_file_with_retry(src, dest_dir, "out.jpg", retry_times=3, retry_delays=[0, 0, 0])
    finally:
        storage.copy_file = original_copy

    assert result.success
    assert result.attempts == 2


# ── 刪除圍籬：全程式唯一可刪除之處，且僅限暫存區 ──────

def test_safe_delete_in_temp_deletes_file_inside_temp(tmp_path):
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    f = temp_root / "a.jpg"
    f.write_bytes(b"x")
    storage.safe_delete_in_temp(f, temp_root)
    assert not f.exists()


def test_safe_delete_in_temp_deletes_dir_inside_temp(tmp_path):
    temp_root = tmp_path / "temp"
    sub = temp_root / "123_秀琴" / "20260723_2140_阿嬤生日"
    sub.mkdir(parents=True)
    (sub / "a.jpg").write_bytes(b"x")
    storage.safe_delete_in_temp(sub, temp_root)
    assert not sub.exists()


def test_safe_delete_refuses_path_outside_temp(tmp_path):
    """刪除圍籬核心測項（對應 H11）：非暫存區一律拒絕執行，即使呼叫端傳入目的地路徑。"""
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    destination = tmp_path / "NAS" / "阿嬤生日"
    destination.mkdir(parents=True)
    victim = destination / "precious.jpg"
    victim.write_bytes(b"irreplaceable")

    with pytest.raises(storage.TempFenceViolation):
        storage.safe_delete_in_temp(victim, temp_root)
    assert victim.exists()
    assert victim.read_bytes() == b"irreplaceable"


def test_safe_delete_refuses_sibling_dir_that_merely_shares_prefix(tmp_path):
    """路徑圍籬須用真正的父子關係判斷，不能只用字串前綴比對。"""
    temp_root = tmp_path / "photo-bot-temp"
    temp_root.mkdir()
    lookalike = tmp_path / "photo-bot-temp-backup"
    lookalike.mkdir()
    victim = lookalike / "a.jpg"
    victim.write_bytes(b"x")

    with pytest.raises(storage.TempFenceViolation):
        storage.safe_delete_in_temp(victim, temp_root)
    assert victim.exists()


def test_safe_delete_nonexistent_path_is_noop(tmp_path):
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    storage.safe_delete_in_temp(temp_root / "ghost.jpg", temp_root)  # 不應拋錯


def test_storage_module_has_no_destination_delete_or_move_calls():
    """
    對應測項 H11：檢視 storage.py 原始碼，除了刪除圍籬函式本身，
    不應出現任何可能作用於任意路徑的刪除/搬移呼叫。
    """
    source = Path(storage.__file__).read_text(encoding="utf-8")
    assert "shutil.move" not in source
    assert "os.remove" not in source
    # shutil.rmtree 只能出現在 safe_delete_in_temp 這個有圍籬保護的函式內
    assert source.count("shutil.rmtree") == 1
    # .unlink() 只能出現兩處：safe_delete_in_temp（圍籬保護）與
    # health_check 自己建立又自己刪除的極小健檢暫存檔（不涉及任何照片）
    assert source.count(".unlink()") == 2


# ── 暫存區路徑結構（§4.3，測項 F8）────────────────────

def test_user_temp_dir_naming():
    p = storage.user_temp_dir(Path("D:/photo-bot-temp"), 123456789, "秀琴")
    assert p.name == "123456789_秀琴"


def test_session_temp_dir_naming():
    user_dir = Path("D:/photo-bot-temp/123456789_秀琴")
    ts = datetime(2026, 7, 23, 21, 40)
    p = storage.session_temp_dir(user_dir, ts, "阿嬤生日")
    assert p.name == "20260723_2140_阿嬤生日"


# ── 健檢 ────────────────────────────────────────────

def test_health_check_success_leaves_no_trace(tmp_path):
    ok, err = storage.health_check(tmp_path)
    assert ok is True
    assert err is None
    assert list(tmp_path.iterdir()) == []


def test_health_check_failure_on_unwritable_path(tmp_path):
    # 用一个不存在且无法建立的路径（父层是文件）模拟写入失败
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    bad_dir = blocker / "cannot_create"
    ok, err = storage.health_check(bad_dir)
    assert ok is False
    assert err


# ── session 側車檔（§4.3 中斷復原策略，記錄目的地供補送使用）─────

def test_write_and_read_session_info_roundtrip(tmp_path):
    info = {"destination": "兩邊都存", "folder": "阿嬤生日", "telegram_id": 123, "name": "秀琴"}
    storage.write_session_info(tmp_path, info)
    assert storage.read_session_info(tmp_path) == info


def test_read_session_info_missing_returns_none(tmp_path):
    assert storage.read_session_info(tmp_path) is None


def test_read_session_info_corrupted_returns_none(tmp_path):
    (tmp_path / storage.SESSION_INFO_FILENAME).write_text("{not valid json", encoding="utf-8")
    assert storage.read_session_info(tmp_path) is None
