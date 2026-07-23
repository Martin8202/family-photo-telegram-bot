import json
import threading
import time
from pathlib import Path

from writequeue import WriteQueue, atomic_write_text


def test_atomic_write_creates_file(tmp_path):
    p = tmp_path / "a.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8-sig") == "hello"


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    p = tmp_path / "a.txt"
    atomic_write_text(p, "hello")
    leftovers = [f for f in tmp_path.iterdir() if f.name != "a.txt"]
    assert leftovers == []


def test_write_queue_serializes_concurrent_writers(tmp_path):
    """§10C：單一背景執行緒依序執行，天然序列化，不會有兩個寫入同時進行。"""
    wq = WriteQueue()
    path = tmp_path / "counter.json"
    path.write_text(json.dumps({"n": 0}), encoding="utf-8")
    lock_free_race_detected = {"flag": False}

    def increment():
        def _do():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            time.sleep(0.005)  # 刻意製造若無序列化就會競爭的窗口
            data["n"] += 1
            atomic_write_text(path, json.dumps(data))
        wq.submit(_do)

    threads = [threading.Thread(target=increment) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = json.loads(path.read_text(encoding="utf-8-sig"))
    assert final["n"] == 20  # 若序列化失效，會因競爭條件而少於 20


def test_write_queue_propagates_errors_to_caller(tmp_path):
    wq = WriteQueue()

    def _boom():
        raise ValueError("寫入失敗")

    try:
        wq.submit(_boom)
        assert False, "應該要拋出例外"
    except ValueError:
        pass
