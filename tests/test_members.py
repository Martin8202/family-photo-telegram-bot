import json
from pathlib import Path

from members import MembersStore, STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED
from writequeue import WriteQueue


def make_store(tmp_path) -> MembersStore:
    wq = WriteQueue()
    return MembersStore(tmp_path / "members.json", write_queue=wq)


# ── 註冊 / 狀態機（§5、測項 A4-A8）───────────────────

def test_register_new_member_is_pending(tmp_path):
    store = make_store(tmp_path)
    m = store.register(123, "秀琴")
    assert m.status == STATUS_PENDING
    assert m.name == "秀琴"
    assert m.telegram_id == 123


def test_register_persists_to_disk(tmp_path):
    store = make_store(tmp_path)
    store.register(123, "秀琴")
    on_disk = json.loads((tmp_path / "members.json").read_text(encoding="utf-8-sig"))
    assert on_disk["123"]["name"] == "秀琴"
    assert on_disk["123"]["status"] == STATUS_PENDING


def test_approve_changes_status(tmp_path):
    store = make_store(tmp_path)
    store.register(123, "秀琴")
    m = store.approve(123)
    assert m.status == STATUS_APPROVED
    assert store.is_approved(123)


def test_reject_changes_status_and_blocks_usage(tmp_path):
    store = make_store(tmp_path)
    store.register(123, "秀琴")
    m = store.reject(123)
    assert m.status == STATUS_REJECTED
    assert not store.is_approved(123)


def test_rejected_member_can_register_again_and_returns_to_pending(tmp_path):
    """被拒絕者可重新登記（§5.1、測項 A8）。"""
    store = make_store(tmp_path)
    store.register(123, "秀琴")
    store.reject(123)
    m = store.register(123, "秀琴")
    assert m.status == STATUS_PENDING


def test_unknown_member_is_not_approved(tmp_path):
    store = make_store(tmp_path)
    assert store.get(999) is None
    assert not store.is_approved(999)


def test_load_existing_members_file(tmp_path):
    path = tmp_path / "members.json"
    path.write_text(
        json.dumps({"123": {"name": "秀琴", "telegram_id": 123, "joined_at": "2026-07-22 10:30",
                             "status": STATUS_APPROVED, "recent_folders": []}}),
        encoding="utf-8",
    )
    store = MembersStore(path, write_queue=WriteQueue())
    assert store.is_approved(123)


def test_corrupted_members_file_does_not_crash(tmp_path):
    path = tmp_path / "members.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = MembersStore(path, write_queue=WriteQueue())
    assert store.list_all() == []


# ── 近 3 次資料夾（§6.2、測項 B7-B9）───────────────────

def test_recent_folders_independent_per_user(tmp_path):
    store = make_store(tmp_path)
    store.register(1, "秀琴")
    store.register(2, "大姊")
    store.push_recent_folder(1, "阿嬤生日", "家裡硬碟")

    assert len(store.get_recent_folders(1)) == 1
    assert store.get_recent_folders(2) == []


def test_recent_folders_capped_and_most_recent_first(tmp_path):
    store = make_store(tmp_path)
    store.register(1, "秀琴")
    for name in ["A", "B", "C", "D"]:
        store.push_recent_folder(1, name, "家裡硬碟", keep=3)
    folders = [f["name"] for f in store.get_recent_folders(1)]
    assert folders == ["D", "C", "B"]  # 最新 3 筆，最新在前


def test_recent_folder_reuse_moves_to_front_without_duplicate(tmp_path):
    store = make_store(tmp_path)
    store.register(1, "秀琴")
    store.push_recent_folder(1, "阿嬤生日", "家裡硬碟")
    store.push_recent_folder(1, "過年聚餐", "OneDrive")
    store.push_recent_folder(1, "阿嬤生日", "兩邊都存")  # 再次使用

    folders = store.get_recent_folders(1)
    assert [f["name"] for f in folders] == ["阿嬤生日", "過年聚餐"]
    assert folders[0]["last_dest"] == "兩邊都存"  # 目的地紀錄為「本次」，非綁定


def test_recent_folder_not_bound_to_destination_is_just_display_hint(tmp_path):
    """「近 3 次資料夾」不綁定目的地，僅記錄名稱供圖示顯示（§6.2、測項 B8）。"""
    store = make_store(tmp_path)
    store.register(1, "秀琴")
    store.push_recent_folder(1, "阿嬤生日", "家裡硬碟")
    folders = store.get_recent_folders(1)
    assert set(folders[0].keys()) >= {"name", "last_dest", "used_at"}
