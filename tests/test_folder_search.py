"""folder_search：日期正規化、比對排序、只掃 2 層、閒置才重掃、掃描中讀舊清單（V2 §3.3、§3.4）。"""

import json
import threading
from datetime import datetime, timedelta

import pytest

import folder_search as fs


@pytest.mark.parametrize("text", ["7/2", "0702", "7月2日", "2026-07-02", "2026/7/2", "20260702", "2026年7月2日"])
def test_date_formats_are_the_same_day(text):
    dates, _ = fs.split_dates(text)
    assert dates and dates[0][1:] == (7, 2)


def test_date_range_and_name_split():
    dates, rest = fs.split_dates("2007-7-2~8金門旅遊")
    assert dates == [(2007, 7, 2)]
    assert rest == "金門旅遊"


def test_year_alone_is_not_a_date():
    assert fs.split_dates("2026演唱會")[0] == []
    assert not fs.has_date("金門之旅")


FOLDERS = [
    {"name": "2026-07-02金門泉州之旅", "path": "2026-07-02金門泉州之旅", "mtime": 300},
    {"name": "2007-7-2~8金門旅遊", "path": "2007-7-2~8金門旅遊", "mtime": 100},
    {"name": "2026-07-26曾心梅演唱會", "path": "2026-07-26曾心梅演唱會", "mtime": 200},
    {"name": "2025-07-02生日", "path": "家人\\2025-07-02生日", "mtime": 400},
]


def test_contains_ranks_newer_first():
    names = [f["name"] for f in fs.match("金門", FOLDERS)]
    assert names[:2] == ["2026-07-02金門泉州之旅", "2007-7-2~8金門旅遊"]


def test_date_only_query():
    names = [f["name"] for f in fs.match("7/2", FOLDERS)]
    assert set(names) == {"2026-07-02金門泉州之旅", "2007-7-2~8金門旅遊", "2025-07-02生日"}


def test_contains_beats_date():
    names = [f["name"] for f in fs.match("0702金門", FOLDERS)]
    assert names[0] == "2026-07-02金門泉州之旅"
    assert names.index("2025-07-02生日") > names.index("2007-7-2~8金門旅遊")


def test_similar_name_found():
    names = [f["name"] for f in fs.match("0702金門之旅", FOLDERS)]
    assert names[0] == "2026-07-02金門泉州之旅"


def test_no_match_and_limit():
    assert fs.match("完全不相干的字", FOLDERS) == []
    many = [{"name": f"金門{i}", "path": f"金門{i}", "mtime": i} for i in range(9)]
    assert len(fs.match("金門", many)) == 5


def test_location_text():
    assert fs.location_text("A") == "（在「照片」裡）"
    assert fs.location_text("上層\\子") == "（在「照片」裡的「上層」裡）"


def test_scan_only_two_levels_and_folders_only(tmp_path):
    (tmp_path / "A" / "B" / "C").mkdir(parents=True)
    (tmp_path / "A" / "x.jpg").write_bytes(b"1")
    (tmp_path / "skip").mkdir()
    paths = {f["path"] for f in fs.scan_folders(tmp_path, depth=2, exclude=["skip"])}
    assert paths == {"A", "A\\B"}


def test_rescan_only_when_dirty_and_idle(tmp_path):
    idx = fs.FolderIndex(tmp_path / "folders.json", tmp_path, idle_min=10)
    assert not idx.load() and idx.dirty           # 沒有 JSON → 要掃一次
    idx.dirty = False
    now = datetime(2026, 11, 7, 12, 0)
    idx.note_activity(now)
    idx.mark_upload_done()
    assert not idx.due(now + timedelta(minutes=9))
    assert idx.due(now + timedelta(minutes=10))
    idx.note_activity(now + timedelta(minutes=9))  # 有人又來用 → 重新等 10 分
    assert not idx.due(now + timedelta(minutes=10))


def test_reads_old_list_during_scan_and_keeps_added(tmp_path, monkeypatch):
    root = tmp_path / "照片"
    (root / "舊的").mkdir(parents=True)
    idx = fs.FolderIndex(tmp_path / "folders.json", root)
    idx.rescan()
    assert [f["path"] for f in idx.folders] == ["舊的"]

    (root / "新的").mkdir()
    started, release = threading.Event(), threading.Event()
    real_scan = fs.scan_folders

    def slow_scan(*a, **k):
        started.set()
        release.wait(5)
        return real_scan(*a, **k)

    monkeypatch.setattr(fs, "scan_folders", slow_scan)
    t = threading.Thread(target=idx.rescan)
    t.start()
    started.wait(5)
    assert [f["path"] for f in idx.folders] == ["舊的"]       # 掃描中：舊清單
    on_disk = json.loads((tmp_path / "folders.json").read_text(encoding="utf-8-sig"))
    assert [f["path"] for f in on_disk["folders"]] == ["舊的"]
    idx.add_folder("機器人建的")                               # 掃描中新建的不能被蓋掉
    release.set()
    t.join(5)
    assert {f["path"] for f in idx.folders} == {"舊的", "新的", "機器人建的"}
    reloaded = fs.FolderIndex(tmp_path / "folders.json", root)
    assert reloaded.load() and len(reloaded.folders) == 3
