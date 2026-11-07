import json
from datetime import datetime
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


# ── 最近資料夾：全家共用（V2 D19、§3.2）───────────────────

def _at(minute: int) -> datetime:
    return datetime(2026, 11, 7, 10, minute)


def test_family_recent_folders_are_shared_across_members(tmp_path):
    """媽媽剛傳過的資料夾，爸爸打開就排在最上面。"""
    store = make_store(tmp_path)
    store.register(1, "媽媽")
    store.register(2, "爸爸")
    store.push_recent_folder(2, "爸爸的舊夾", used_at=_at(1))
    store.push_recent_folder(1, "金門泉州之旅", used_at=_at(5))

    assert [f["name"] for f in store.family_recent_folders()] == ["金門泉州之旅", "爸爸的舊夾"]


def test_family_recent_folders_dedup_keeps_latest_and_caps(tmp_path):
    store = make_store(tmp_path)
    store.register(1, "媽媽")
    store.register(2, "爸爸")
    for i, name in enumerate(["A", "B", "C", "D", "E", "F"]):
        store.push_recent_folder(1, name, used_at=_at(i))
    store.push_recent_folder(2, "A", used_at=_at(30))  # 爸爸最近又用了 A

    names = [f["name"] for f in store.family_recent_folders(5)]
    assert names == ["A", "F", "E", "D", "C"]  # 同一個資料夾只出現一次，取最新；最多 5 個


def test_recent_folder_reuse_moves_to_front_and_keep_cap(tmp_path):
    store = make_store(tmp_path)
    store.register(1, "秀琴")
    for i, name in enumerate(["A", "B", "C", "A"]):
        store.push_recent_folder(1, name, used_at=_at(i), keep=2)
    assert [f["name"] for f in store.get(1).recent_folders] == ["A", "C"]
    assert set(store.get(1).recent_folders[0]) == {"name", "used_at"}  # V2 不再記目的地


def test_migrate_recent_folders_keeps_existing_drops_missing(tmp_path):
    """V1 舊紀錄（帶 last_dest）：照片底下找得到才留、拿掉 last_dest；新紀錄不動。"""
    store = make_store(tmp_path)
    store.register(1, "媽媽")
    store._data["1"]["recent_folders"] = [
        {"name": "金門泉州之旅", "last_dest": "OneDrive", "used_at": "2026-09-08 10:00"},
        {"name": "只在OneDrive", "last_dest": "OneDrive", "used_at": "2026-09-01 10:00"},
        {"name": "V2新紀錄", "used_at": "2026-11-07 10:00"},
    ]
    dropped = store.migrate_recent_folders(lambda name: name == "金門泉州之旅")

    assert dropped == ["只在OneDrive"]
    assert store.get(1).recent_folders == [
        {"name": "金門泉州之旅", "used_at": "2026-09-08 10:00"},
        {"name": "V2新紀錄", "used_at": "2026-11-07 10:00"},
    ]
    assert store.migrate_recent_folders(lambda name: False) == [], "重跑不可以再刪新格式的紀錄"
