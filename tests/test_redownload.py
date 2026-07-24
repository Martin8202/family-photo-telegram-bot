"""redownload.py 的分組去重邏輯（review 問題二）。"""

from writequeue import WriteQueue
from logs import DataLogs
import redownload


def test_load_groups_dedups_same_file_id_from_both_destinations(tmp_path):
    """
    「📦 兩邊都存」會讓同一張照片在 file_index 產生兩列（家裡硬碟 + OneDrive），
    但 file_id 相同。load_groups 須依 file_id 去重，避免重複下載與張數加倍。
    """
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    # 同一張照片、兩個目的地、相同 file_id
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1, "阿嬤生日", "家裡硬碟", "a.jpg", "FILEID_A")
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1, "阿嬤生日", "OneDrive", "a.jpg", "FILEID_A")
    # 另一張照片，只存一邊
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1, "阿嬤生日", "家裡硬碟", "b.jpg", "FILEID_B")

    groups = redownload.load_groups(tmp_path)
    key = ("阿嬤生日", "秀琴", "2026-07-24")
    assert key in groups
    file_ids = [r["file_id"] for r in groups[key]]
    assert file_ids == ["FILEID_A", "FILEID_B"]  # 去重後兩張，非三列


def test_load_groups_separates_different_days(tmp_path):
    logs = DataLogs(tmp_path, write_queue=WriteQueue())
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1, "阿嬤生日", "家裡硬碟", "a.jpg", "ID1")
    logs.log_file_index("2026-07-25 10:00", "秀琴", 1, "阿嬤生日", "家裡硬碟", "b.jpg", "ID2")
    groups = redownload.load_groups(tmp_path)
    assert ("阿嬤生日", "秀琴", "2026-07-24") in groups
    assert ("阿嬤生日", "秀琴", "2026-07-25") in groups
