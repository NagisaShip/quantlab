"""`JsonStore` 的跨进程读写与锁重入（T12 发现的真实缺陷的回归测试）。

缺陷背景（Windows 上稳定复现，Linux 因 flock 语义宽松不易触发）：
`upsert` 走 `with self._lock, self._xlock(collection)`，写文件用
`tmp.write_text(...)` + `tmp.replace(path)`；而 **读**路径 `_load()` 只拿线程锁，
直接 `path.read_text()`。于是 API 进程正在读 `roles.json` 时，worker 进程执行
`os.replace` 就会抛 `PermissionError: [WinError 5]` / `[Errno 13]`。

两条修复各有一个测试：
1. `_load()` 读盘时也进 `_xlock`，读者不再"开着文件跟替换撞车"；
2. `_xlock` 支持**同线程重入** —— 否则 `upsert` 持锁后调 `_load` 再拿同一把锁，
   Windows 的 `msvcrt.locking(LK_LOCK)` 会自己等自己（死锁）。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from quant_agent.store import JsonStore


def test_upsert_is_reentrant_on_the_file_lock(tmp_path):
    """`upsert` 在持锁状态下调 `_load`、`_load` 又要拿同一把锁，必须不死锁。

    这是 Windows 上的死锁点：`msvcrt.locking(LK_LOCK)` 对同一字节区间重复加锁
    会阻塞自己，所以 `_FileLock` 必须显式做同线程重入计数。
    """
    store = JsonStore(tmp_path)

    done = threading.Event()
    err: list[BaseException] = []

    def run() -> None:
        try:
            for i in range(20):
                store.upsert("roles", {"id": f"r{i}", "name": f"角色{i}"})
        except BaseException as exc:
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # 20 次 upsert 正常应在毫秒级；5 秒还没结束就说明锁自己等自己了。
    assert done.wait(timeout=5), "upsert 疑似死锁：同线程重复获取跨进程文件锁时阻塞"
    assert err == []
    assert store.count("roles") == 20


def test_nested_xlock_does_not_deadlock(tmp_path):
    """显式嵌套两层 `_xlock` 也要能进出（读路径会在写路径的锁内再拿一次）。"""
    store = JsonStore(tmp_path)
    store.upsert("roles", {"id": "a"})

    with store._xlock("roles"):
        with store._xlock("roles"):
            rows = store._load("roles")
        assert [r["id"] for r in rows] == ["a"]
    # 出锁之后必须真的释放了：另一个"进程"（这里用另一把锁对象，走同一文件）
    # 还能拿到锁，说明没有把锁泄漏在已获取状态。
    with store._xlock("roles"):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="该竞争只在 Windows 上稳定复现")
def test_concurrent_reader_never_gets_permission_error(tmp_path):
    """读者与写者并发时，读者不得抛 `PermissionError`（T12 的真实 500 根因）。

    这里必须用**独立子进程**，不能用线程：修复前 `_load()` 只拿线程锁、不拿跨进程锁，
    而 `_flush()` 的 `os.replace` 走的是跨进程锁。同进程内的两个线程会被同一个
    `self._lock`（RLock）串行化，竞争体现不出来；只有"进程 A 读、进程 B 替换"
    才会在 Windows 上稳定触发 `PermissionError: [Errno 13]`。
    （实测：还原修复后 7 秒内两个读者分别报错 17 次 / 11 次；修复后 0 次。）
    """
    import multiprocessing as mp

    store = JsonStore(tmp_path)
    store.upsert("roles", {"id": "hot", "n": 0})

    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    seconds = 6.0
    procs = [
        ctx.Process(target=_hammer_writer, args=(str(tmp_path), seconds)),
        ctx.Process(target=_hammer_reader, args=(str(tmp_path), seconds, "r1")),
        ctx.Process(target=_hammer_reader, args=(str(tmp_path), seconds, "r2")),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"子进程异常退出：exitcode={p.exitcode}"

    errs: list[str] = []
    for name in ("r1", "r2", "writer"):
        errs += json.loads((tmp_path / f"errs_{name}.json").read_text(encoding="utf-8"))
    assert errs == [], f"并发读写时读者抛了 {len(errs)} 次异常：{errs[:3]}"

    # 文件必须仍然是完整可解析的 JSON（原子替换没有把它写坏）。
    data = json.loads((tmp_path / "roles.json").read_text(encoding="utf-8"))
    assert any(r["id"] == "hot" for r in data)


def _hammer_reader(root: str, seconds: float, tag: str) -> None:
    """子进程：不停读，把所有异常写进 errs_<tag>.json 供父进程断言。"""
    s = JsonStore(root)
    end = time.time() + seconds
    errs: list[str] = []
    while time.time() < end:
        try:
            s.list("roles")
            s.get("roles", "hot")
            assert s.count("roles") >= 1
        except BaseException as exc:
            errs.append(f"{type(exc).__name__}: {exc}")
    (Path(root) / f"errs_{tag}.json").write_text(json.dumps(errs), encoding="utf-8")


def _hammer_writer(root: str, seconds: float) -> None:
    """子进程：不停写（触发 os.replace），同样记录自己的异常。"""
    s = JsonStore(root)
    end = time.time() + seconds
    errs: list[str] = []
    n = 0
    while time.time() < end:
        try:
            s.upsert("roles", {"id": "hot", "n": n})
            n += 1
        except BaseException as exc:
            errs.append(f"{type(exc).__name__}: {exc}")
    (Path(root) / "errs_writer.json").write_text(json.dumps(errs), encoding="utf-8")


def test_replace_retries_when_target_is_locked(tmp_path, monkeypatch):
    """`_flush` 遇到"目标文件被占用"时要重试，而不是立刻把 500 抛给用户。

    模拟 Windows 的瞬时占用：前两次 `replace` 抛 `PermissionError`，
    第三次成功 —— 写入最终必须成功且内容正确。
    """
    store = JsonStore(tmp_path)
    store.upsert("roles", {"id": "a"})

    real_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self: Path, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "Permission denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    store.upsert("roles", {"id": "b"})

    assert calls["n"] == 3, f"应重试到第 3 次才成功，实际调用 {calls['n']} 次"
    rows = json.loads((tmp_path / "roles.json").read_text(encoding="utf-8"))
    assert sorted(r["id"] for r in rows) == ["a", "b"]


def test_replace_gives_up_after_retries(tmp_path, monkeypatch):
    """重试次数用尽后必须**照常抛出**，不能把错误吞掉变成静默丢数据。"""
    store = JsonStore(tmp_path)
    store.upsert("roles", {"id": "a"})

    def always_fail(self: Path, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "replace", always_fail)
    with pytest.raises(PermissionError):
        store.upsert("roles", {"id": "b"})
