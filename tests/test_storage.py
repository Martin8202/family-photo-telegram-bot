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
    # 時間戳採 EXIF 拍攝時間，字尾採檔案內容 MD5 前 8 碼
    assert name.startswith("20080227_143015_")
    assert name.endswith(storage.content_fingerprint(img_path) + ".jpg")


def test_build_filename_same_content_same_name(tmp_path):
    """同一張照片（內容相同）無論何時傳送，都必須得到完全相同的檔名。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"SAME-CONTENT")
    copy = tmp_path / "b.jpg"
    copy.write_bytes(b"SAME-CONTENT")

    received = datetime(2026, 7, 25, 12, 0, 0, 123456)
    n1 = storage.build_filename(received, ext=".jpg", source_path=img, use_exif=False)
    n2 = storage.build_filename(received, ext=".jpg", source_path=copy, use_exif=False)
    assert n1 == n2
    assert n1.endswith(storage.calculate_md5(img)[:8] + ".jpg")


def test_build_filename_different_photos_get_different_fingerprints(tmp_path):
    """
    實測 bug 回歸：同一秒收到的不同照片必須拿到**不同**的指紋。

    原本的實作取 `file_unique_id[-8:]`，但那是 base64 結構的固定尾段——
    實測 24 張照片有 23 張都算出同一個指紋 `aaifkivc`，於是同一秒的照片
    全部撞名，一路 `_(2)`、`_(3)`、`_(4)` 疊上去。
    """
    received = datetime(2026, 7, 25, 15, 10, 54, 0)  # 全部同一秒，且無 EXIF
    names = set()
    for i in range(24):
        p = tmp_path / f"photo_{i}.jpg"
        p.write_bytes(f"different photo content {i}".encode())
        names.add(storage.build_filename(received, ext=".jpg", source_path=p, use_exif=False))
    assert len(names) == 24, f"24 張不同照片應該得到 24 個不同檔名，實際只有 {len(names)} 個"


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
    """
    §2／§8／§10 的硬性保證：目的地已有同名檔案時一律另存，絕不覆蓋。
    覆蓋是破壞性寫入（copy2 底層 open(dst,'wb') 開檔當下原檔就歸零），
    途中斷線會同時失去原檔與新檔——這條測試就是守這個的，不可以反過來寫。
    """
    src1 = tmp_path / "src1.jpg"
    src1.write_bytes(b"AAA")
    src2 = tmp_path / "src2.jpg"
    src2.write_bytes(b"BBB")
    dest_dir = tmp_path / "dest"

    p1 = storage.copy_file(src1, dest_dir, "same.jpg")
    p2 = storage.copy_file(src2, dest_dir, "same.jpg")

    assert p1 != p2, "撞名必須另存新檔，不可指向同一個路徑"
    assert p1.read_bytes() == b"AAA", "原本那張照片必須完好無損"
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


# ── 資料夾名稱驗證（實測 WinError 123 的回歸測試）────────────────

def test_validate_folder_name_rejects_newline():
    """
    實測踩到的 crash：使用者輸入「2026-07-025大量測試」時中間夾了換行
    （手機輸入法斷行或複製貼上帶進來），舊版的過濾器不擋換行，直接把它
    帶進路徑，Windows 回 WinError 123 讓整個上傳流程中斷。
    """
    with pytest.raises(storage.FolderNameError) as exc:
        storage.validate_folder_name("2026-07-0\n25大量測試")
    assert "換行" in str(exc.value)


def test_validate_folder_name_rejects_other_control_chars():
    for bad in ["a\tb", "a\rb", "a\x00b"]:
        with pytest.raises(storage.FolderNameError):
            storage.validate_folder_name(bad)


def test_validate_folder_name_rejects_path_chars_and_names_them():
    with pytest.raises(storage.FolderNameError) as exc:
        storage.validate_folder_name("阿嬤/生日")
    msg = str(exc.value)
    assert "/" in msg and "換一個名字" in msg


def test_validate_folder_name_rejects_empty_and_whitespace_only():
    for bad in ["", "   "]:
        with pytest.raises(storage.FolderNameError):
            storage.validate_folder_name(bad)


def test_validate_folder_name_rejects_trailing_dot_and_reserved_and_too_long():
    with pytest.raises(storage.FolderNameError):
        storage.validate_folder_name("過年聚餐.")
    with pytest.raises(storage.FolderNameError):
        storage.validate_folder_name("CON")
    with pytest.raises(storage.FolderNameError):
        storage.validate_folder_name("長" * (storage.MAX_FOLDER_NAME_LENGTH + 1))


def test_validate_folder_name_accepts_normal_names_and_trims():
    assert storage.validate_folder_name("  阿嬤生日  ") == "阿嬤生日"
    assert storage.validate_folder_name("2026-07-25大量測試") == "2026-07-25大量測試"
    assert storage.validate_folder_name("110嘉義家族旅遊") == "110嘉義家族旅遊"


def test_sanitize_folder_name_never_produces_invalid_path():
    """sanitize 是內部用（例如成員姓名組暫存夾），不能失敗，但也絕不能吐出非法路徑。"""
    assert "\n" not in storage.sanitize_folder_name("元\n皓")
    assert storage.sanitize_folder_name("元\n皓") == "元 皓"
    assert storage.sanitize_folder_name("結尾點.") == "結尾點"
    assert storage.sanitize_folder_name("CON") == "CON_"
    assert len(storage.sanitize_folder_name("長" * 500)) <= storage.MAX_FOLDER_NAME_LENGTH


# ── 指紋反查與重複偵測（實測 bug 回歸）────────────────────────

def test_extract_fingerprint_from_filename():
    assert storage.extract_fingerprint("20260725_151054_892e347b.JPG") == "892e347b"
    assert storage.extract_fingerprint("20260725_151054_892e347b_(2).JPG") == "892e347b"
    assert storage.extract_fingerprint("20260725_151054_892e347b_(13).HEIC") == "892e347b"
    # 認不出格式的（使用者自己放進去的檔案）要回傳 None，不可誤判
    assert storage.extract_fingerprint("我的照片.jpg") is None
    assert storage.extract_fingerprint("IMG_1234.JPG") is None
    assert storage.extract_fingerprint("20260725_151054_XYZ.jpg") is None


def test_scan_existing_fingerprints(tmp_path):
    d = tmp_path / "相簿"
    d.mkdir()
    (d / "20260725_151054_892e347b.JPG").write_bytes(b"a")
    (d / "20260101_090000_deadbeef.PNG").write_bytes(b"b")
    (d / "20260725_151054_892e347b_(2).JPG").write_bytes(b"c")  # 同指紋的副本
    (d / "使用者自己放的.jpg").write_bytes(b"d")

    found = storage.scan_existing_fingerprints(d)
    assert set(found) == {"892e347b", "deadbeef"}
    # 一併帶回檔案大小，用來排除指紋碰撞造成的誤判
    assert found["892e347b"] == {1}   # 兩個同指紋的檔案都是 1 byte
    assert found["deadbeef"] == {1}
    assert storage.scan_existing_fingerprints(tmp_path / "不存在") == {}


def test_looks_like_duplicate_requires_size_match():
    """
    指紋只有 32 bits，理論上有極小機率碰撞。若因此誤判成重複而略過複製，
    那張全新的照片就不會被存——違反「照片不遺失」。故必須大小也吻合。
    """
    known = {"892e347b": {1024}}
    assert storage.looks_like_duplicate(known, "892e347b", 1024) is True
    # 指紋一樣但大小不同 → 是碰撞，不是重複，必須照常複製
    assert storage.looks_like_duplicate(known, "892e347b", 2048) is False
    assert storage.looks_like_duplicate(known, "deadbeef", 1024) is False
    # 資訊不齊時一律當作「不是重複」，寧可多存也不要漏存
    assert storage.looks_like_duplicate(known, None, 1024) is False
    assert storage.looks_like_duplicate(known, "892e347b", None) is False


def test_same_photo_different_receive_time_has_different_name_but_same_fingerprint(tmp_path):
    """
    這就是重複偵測不能只看檔名的原因：讀不到 EXIF 時（Telegram 壓縮版的 EXIF
    會被剝掉），時間戳退回接收時間，同一張照片分兩次上傳檔名就不同了。
    但**指紋必須相同**——重複偵測要靠它。
    """
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"IDENTICAL-PHOTO-CONTENT")

    n1 = storage.build_filename(datetime(2026, 7, 25, 15, 10, 54, 1), ext=".jpg",
                                source_path=img, use_exif=True)
    n2 = storage.build_filename(datetime(2026, 8, 1, 9, 30, 0, 2), ext=".jpg",
                                source_path=img, use_exif=True)

    assert n1 != n2, "沒有 EXIF 時，兩次上傳的檔名本來就會不同"
    assert storage.extract_fingerprint(n1) == storage.extract_fingerprint(n2), \
        "但指紋必須相同，重複偵測才抓得到"


def test_heic_support_registered():
    """iPhone 原檔是 HEIC，需要 pillow-heif 才讀得到 EXIF 拍攝時間（§10）。"""
    assert storage.HEIC_SUPPORTED, "pillow-heif 應該已安裝並註冊"
