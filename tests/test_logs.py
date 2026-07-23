import csv
import threading
from pathlib import Path

from logs import DataLogs
from writequeue import WriteQueue


def test_csv_files_created_with_header(tmp_path):
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    assert (tmp_path / "upload_log.csv").exists()
    assert (tmp_path / "待清理清單.csv").exists()
    assert (tmp_path / "file_index.csv").exists()

    with open(tmp_path / "upload_log.csv", encoding="utf-8-sig") as f:
        header = next(csv.reader(f))
    assert header == ["時間", "上傳者", "Telegram ID", "資料夾", "目的地", "張數", "結果"]


def test_upload_log_row_appended(tmp_path):
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    logs.log_upload("2026-07-22 21:40", "秀琴", 123456789, "110嘉義家族旅遊", "兩邊都存", 100, "成功")
    rows = logs.upload_log.read_all_rows()
    assert len(rows) == 1
    assert rows[0]["上傳者"] == "秀琴"
    assert rows[0]["張數"] == "100"


def test_three_files_stay_independent(tmp_path):
    """§10D：三個檔案彼此獨立、各自成檔，不可混寫。"""
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    logs.log_upload("t", "秀琴", 1, "A", "家裡硬碟", 1, "成功")
    logs.log_cleanup("t", "秀琴", 1, "中止殘留", r"\\nas\A", "a.jpg")
    logs.log_file_index("t", "秀琴", 1, "A", "家裡硬碟", "a.jpg", "FILEID123")

    assert len(logs.upload_log.read_all_rows()) == 1
    assert len(logs.cleanup_list.read_all_rows()) == 1
    assert len(logs.file_index.read_all_rows()) == 1
    # 內容不互相混雜
    assert "file_id" not in logs.upload_log.read_all_rows()[0]
    assert "張數" not in logs.cleanup_list.read_all_rows()[0]


def test_file_index_one_row_per_photo(tmp_path):
    """§16.1：一張照片對應一個 file_id，一對一，不合併（測項 F4）。"""
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    rows = [("t", "秀琴", 1, "A", "家裡硬碟", f"{i}.jpg", f"FILEID{i}") for i in range(10)]
    logs.log_file_index_batch(rows)
    assert len(logs.file_index.read_all_rows()) == 10


def test_program_only_appends_never_truncates_existing_rows(tmp_path):
    """§10B：程式只會新增，不會自行清空或刪除既有內容。"""
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    logs.log_cleanup("t1", "秀琴", 1, "中止殘留", "loc", "a.jpg")
    logs.log_cleanup("t2", "秀琴", 1, "傳錯更正", "loc", "b.jpg")
    rows = logs.cleanup_list.read_all_rows()
    assert [r["檔名"] for r in rows] == ["a.jpg", "b.jpg"]


def test_concurrent_writes_do_not_corrupt_or_lose_rows(tmp_path):
    """§10C / 測項 F7：多執行緒併發寫入同一 CSV，內容不損毀、無錯亂列、無遺漏。"""
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    n = 40

    def writer(i):
        logs.log_upload(f"t{i}", f"user{i}", i, "A", "家裡硬碟", 1, "成功")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = logs.upload_log.read_all_rows()
    assert len(rows) == n
    uploaders = {r["上傳者"] for r in rows}
    assert uploaders == {f"user{i}" for i in range(n)}


def test_csv_readable_with_bom_for_excel(tmp_path):
    """§F6：CSV 可用 Excel 開啟，中文不亂碼（以 UTF-8 BOM 確認）。"""
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    logs.log_upload("t", "秀琴", 1, "阿嬤生日", "家裡硬碟", 1, "成功")
    raw = (tmp_path / "upload_log.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "阿嬤生日".encode("utf-8") in raw
