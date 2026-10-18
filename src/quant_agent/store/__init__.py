"""轻量持久化层。

一期用 JSON 文件（配置、角色、团队、策略版本、实验记录、Agent 运行轨迹）。
plan.md 规划的 PostgreSQL / Parquet 在数据量上来后替换本层实现，
接口保持不变（见 Store 的方法签名）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_ROOT = Path.home() / ".quant_agent" / "store"

#: `_flush()` 里 `os.replace` 被占用时的重试次数。
#: Windows 上"读者正打开目标文件"会让替换失败（WinError 5），而读者的读窗口极短，
#: 所以重试几次就够；重试耗尽后照常抛出，不吞异常。见 `_flush` 的注释。
_REPLACE_RETRIES = 10


class _LockDepth(NamedTuple):
    """某个线程当前持有的 `_FileLock`：路径、重入层数、以及要复用的文件句柄。"""

    path: Path
    count: int
    fh: Any


#: 线程局部的跨进程锁持有状态（`_XLOCK_DEPTH.depth`）。
#: 用 `threading.local` 而不是全局 dict：重入判定必须**按线程**，
#: 否则 A 线程持锁会让 B 线程误以为可以重入。
_XLOCK_DEPTH: Any = threading.local()


class _FileLock:
    """跨进程互斥（API 与 worker 是两个进程，都会写同一个集合文件）。POSIX 用 flock，Windows 用 msvcrt。

    **同一线程可重入**：`upsert`/`delete` 已经拿着 `_xlock` 再调 `_load`，而 `_load`
    读盘时也要拿同一把锁。`flock` 在同一进程内重复加锁不会阻塞（同一 fd 表项），
    但 `msvcrt.locking(LK_LOCK)` 会**自己等自己**——所以必须显式记住重入次数。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = None

    def __enter__(self):
        # 同线程重入：只累加计数，不再去要系统锁。
        depth = getattr(_XLOCK_DEPTH, "depth", None)
        if depth is not None and depth.path == self.path:
            _XLOCK_DEPTH.depth = _LockDepth(self.path, depth.count + 1, depth.fh)
            self.fh = depth.fh
            return self
        self.fh = open(self.path, "a+b")
        try:
            import fcntl

            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            import msvcrt

            self.fh.seek(0)
            while True:
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        _XLOCK_DEPTH.depth = _LockDepth(self.path, 1, self.fh)
        return self

    def __exit__(self, *exc):
        depth = getattr(_XLOCK_DEPTH, "depth", None)
        if depth is not None and depth.path == self.path and depth.count > 1:
            _XLOCK_DEPTH.depth = _LockDepth(self.path, depth.count - 1, depth.fh)
            return
        _XLOCK_DEPTH.depth = None
        try:
            try:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.fh.close()


class JsonStore:
    """JSON 集合存储：一个集合 = 一个 json 文件。

    多进程安全：读按文件签名（mtime + 大小）判断缓存是否过期；写在跨进程文件锁内
    “重新读取 → 修改 → 原子替换”，API 与 worker 同时写也不会互相覆盖。
    """

    def __init__(self, root: str | Path | None = None, *, results_root: str | Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        # 结果对象存储的根目录：T06 要求路径是 `<store_root>/results/<workspace>/<run>.<格式>`。
        # 默认放在本 store 目录下的 results/；WorkspaceStore 会显式指到数据目录一级
        # （`<数据目录>/results`），这样多个工作区的结果并排而不是各自嵌套在 store 里。
        self.results_root = Path(results_root) if results_root else self.root / "results"
        self._lock = threading.RLock()
        self._cache: dict[str, list[dict]] = {}
        self._sig: dict[str, tuple[int, int, int]] = {}

    def _path(self, collection: str) -> Path:
        return self.root / f"{collection}.json"

    def _signature(self, path: Path) -> tuple[int, int, int]:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return (0, 0, 0)

    def _load(self, collection: str) -> list[dict]:
        """读取集合。**缓存命中时不碰文件**，所以不需要跨进程锁。

        只有真要读盘的那一次才进 `_xlock`：这不是为了防"读到写了一半的内容"
        （写走的是原子替换，读者要么看到旧的要么看到新的），而是为了**不把文件开着
        跟写者的 `os.replace` 撞车**——Windows 上这正是 `PermissionError: [WinError 5]`
        的来源（API 与 worker 是两个进程，worker 启动就写 roles，读者恰好同时在读）。
        """
        path = self._path(collection)
        sig = self._signature(path)
        if collection in self._cache and self._sig.get(collection) == sig:
            return self._cache[collection]
        with self._xlock(collection):
            # 拿到锁后重新取签名：等锁期间文件可能已经被别的进程换掉了，
            # 用旧签名会把过期内容写回缓存（上面那次 stat 只是快速路径）。
            sig = self._signature(path)
            if collection in self._cache and self._sig.get(collection) == sig:
                return self._cache[collection]
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = []
            else:
                data = []
            self._cache[collection] = data
            self._sig[collection] = sig
            return data

    def _flush(self, collection: str) -> None:
        path = self._path(collection)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(self._cache[collection], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for attempt in range(_REPLACE_RETRIES):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                # Windows：另一个进程正 `read_text()` 打开着目标文件时，`os.replace`
                # 会直接抛 WinError 5（拒绝访问）——它不是"锁没拿到"，而是"文件正被占用"，
                # 所以拿跨进程锁也救不了读路径（读也在跨进程锁外，见 `_load`）。
                # 读窗口极短（读一个 json 文件），短暂重试即可；一直失败就抛出去，
                # 由调用方按正常错误路径处理，而不是在这里悄悄吞掉。
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
        self._sig[collection] = self._signature(path)

    def _xlock(self, collection: str) -> _FileLock:
        return _FileLock(self.root / f".{collection}.lock")

    # ---- CRUD ----
    def list(self, collection: str, *, limit: int | None = None) -> list[dict]:
        with self._lock:
            items = list(self._load(collection))
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return items[:limit] if limit else items

    def get(self, collection: str, item_id: str) -> dict | None:
        with self._lock:
            for item in self._load(collection):
                if item.get("id") == item_id:
                    return dict(item)
        return None

    def upsert(self, collection: str, item: dict) -> dict:
        with self._lock, self._xlock(collection):
            items = self._load(collection)
            item = dict(item)
            item.setdefault("id", uuid.uuid4().hex[:12])
            item.setdefault("created_at", time.time())
            item["updated_at"] = time.time()
            for i, existing in enumerate(items):
                if existing.get("id") == item["id"]:
                    items[i] = item
                    break
            else:
                items.append(item)
            self._flush(collection)
            return dict(item)

    def delete(self, collection: str, item_id: str) -> bool:
        with self._lock, self._xlock(collection):
            items = self._load(collection)
            for i, existing in enumerate(items):
                if existing.get("id") == item_id:
                    items.pop(i)
                    self._flush(collection)
                    return True
        return False

    def count(self, collection: str) -> int:
        with self._lock:
            return len(self._load(collection))

    # ---- 大对象：每条一个文件（回测详情、优化结果等），集合里只放索引行 ----
    def _blob_path(self, kind: str, item_id: str) -> Path:
        safe = "".join(ch for ch in item_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("非法 id")
        folder = self.root / "blobs" / kind
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{safe}.json"

    def put_blob(self, kind: str, item_id: str, data: dict) -> None:
        path = self._blob_path(kind, item_id)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        # 与 _flush 相同：Windows 上目标文件正被读（例如对话的实时推送每 0.25 秒读一次）时 replace 会拒绝访问，短暂重试
        for attempt in range(_REPLACE_RETRIES):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.02 * (attempt + 1))

    def get_blob(self, kind: str, item_id: str) -> dict | None:
        """读一个 blob（**面向调用方的完整视图**）。

        若这个 blob 带 `result_ref`（即它只是**摘要 + 引用**，大数组在对象存储里），
        这里会**透明地**把大数组合并回来再返回——调用方拿到的仍是完整结果，
        与 T06 之前的行为一致，不需要改任何读取方。判据是 blob 自带的 `result_ref` 键，
        不是硬编码的 kind，因此其他类型的 blob（回测详情、优化结果）完全不受影响。
        """
        data = self._blob_raw(kind, item_id)
        return None if data is None else self._rehydrate(item_id, data)

    def _blob_raw(self, kind: str, item_id: str) -> dict | None:
        """读 blob 的**原始内容**（不合并大数组）。看落盘摘要、取引用时用它。"""
        try:
            path = self._blob_path(kind, item_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _rehydrate(self, item_id: str, data: dict) -> dict:
        """把"摘要 + 引用"还原成完整结果；没有引用时原样返回。"""
        ref = data.get("result_ref")
        if not isinstance(ref, dict) or not ref.get("workspace_id"):
            return data
        nested = ref.get("nested")
        # 大数组挂在子对象下时：只还原那个子对象，外层其余字段原样保留
        holder = data[nested] if nested and isinstance(data.get(nested), dict) else data
        summary = {k: v for k, v in holder.items() if k != "result_ref"}
        merged = self._results().load_full(ref["workspace_id"], item_id, summary)
        merged = {k: v for k, v in merged.items() if k != "result_ref"}
        if not nested:
            return merged
        out = {k: v for k, v in data.items() if k != "result_ref"}
        out[nested] = merged
        return out

    def delete_blob(self, kind: str, item_id: str) -> bool:
        try:
            path = self._blob_path(kind, item_id)
        except ValueError:
            return False
        if path.exists():
            path.unlink()
            return True
        return False

    # ---- 结果对象存储（T06）：大数组落盘，集合/blob 里只留摘要与引用 ----
    def _results(self):
        """延迟构造结果存储门面（避免模块级导入 platform.results 造成循环依赖）。"""
        cache = self.__dict__.get("_results_cache")
        if cache is None or cache.root != self.results_root:
            from ..platform.results import ResultsStore

            cache = ResultsStore(self.results_root)
            self.__dict__["_results_cache"] = cache
        return cache

    def put_result(self, kind: str, item_id: str, body: dict, *, workspace_id: str, nested: str | None = None) -> dict:
        """把大结果写入对象存储，只把**摘要 + 引用**留在 blob 里。

        `nested`：大数组不在顶层、而是挂在一个子对象下时指定该子对象的键名
        （模板回测的结果就是 `{"run": {…大数组…}, "run_id": …}`）。此时只拆这个子对象，
        读回时原样嵌回，整体形状不变。

        返回存进 blob 的那份内容（摘要 + `result_ref`）。
        """
        target = body[nested] if nested else body
        summary, ref = self._results().put(workspace_id, item_id, target)
        stored: dict = {**body, nested: summary} if nested else dict(summary)
        # kind / nested 一并记进引用：删除时能定位，读回时知道该嵌回哪里
        ref_dict = {**ref.to_dict(), "kind": kind}
        if nested:
            ref_dict["nested"] = nested
        stored["result_ref"] = ref_dict
        self.put_blob(kind, item_id, stored)
        return stored

    def get_result(self, kind: str, item_id: str) -> dict | None:
        """读回完整结果（摘要 + 对象存储里的大数组），与写入前**逐元素一致**。

        与 `get_blob` 等价：`get_blob` 已经会透明合并大数组。保留这个名字是为了让
        "我要的是一份结果"在调用点显式可读。返回对象里**不含** `result_ref`，
        因此与 T06 之前一次性返回的 result 完全相同（验收要求逐元素相等）。
        """
        return self.get_blob(kind, item_id)

    def result_ref(self, kind: str, item_id: str) -> dict | None:
        """只要引用（路径、格式、行数、校验和），不读大文件。

        刻意走 `_blob_raw`：`get_blob` 会把引用本身剔掉，用它是取不到引用的。
        """
        ref = (self._blob_raw(kind, item_id) or {}).get("result_ref")
        return dict(ref) if isinstance(ref, dict) else None

    def delete_result(self, kind: str, item_id: str) -> bool:
        """删除结果大数组与对应 blob；结果引用里记录了真实工作区。"""
        ref = self.result_ref(kind, item_id)
        deleted = False
        if ref and ref.get("workspace_id"):
            try:
                deleted = self._results().delete(str(ref["workspace_id"]), item_id)
            except Exception:
                deleted = False
        return self.delete_blob(kind, item_id) or deleted

    def result_page(self, kind: str, item_id: str, name: str, *, offset: int = 0, limit: int = 2000) -> dict:
        """分页读一个大数组。工作区 id 从 blob 里的引用取，天然限定在本工作区内。"""
        ref = self.result_ref(kind, item_id)
        if ref is None:
            from ..platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.NOT_FOUND, "该结果没有落盘的大数据，无法分页读取")
        return self._results().page(ref["workspace_id"], item_id, name, offset=offset, limit=limit)


def default_store() -> JsonStore:
    return JsonStore()


# ---- 集合名常量，避免拼写漂移 ----
class Collections:
    ROLES = "roles"
    TEAMS = "teams"
    TEAM_RUNS = "team_runs"
    STRATEGIES = "strategies"
    RUNS = "runs"
    AGENT_RUNS = "agent_runs"
    NOTIFICATIONS = "notifications"
    PROVIDERS = "providers"
    SETTINGS = "settings"
    # ---- 统一赛道相关 ----
    BATCHES = "batches"  # 冻结的实验批次：赛道 + 数据 + 全部成绩
    DATASETS = "datasets"  # 上传并校验过的 CSV 行情快照
    # ---- 策略工作室相关 ----
    PAPER_SESSIONS = "paper_sessions"  # 实时模拟实验（含观测点与成交）
    RECONCILIATIONS = "reconciliations"  # 实盘对照报告
    # ---- 研究中心 ----
    BACKTESTS = "backtests"  # 回测记录索引（详情在 blobs/backtests/）
    OPTIMIZATIONS = "optimizations"  # 参数优化记录（详情在 blobs/optimizations/）
    # ---- 策略广场 ----
    POSTS = "posts"  # 帖子（文章 / 问答 / 策略分享）
    COMMENTS = "comments"  # 评论与回复
    PROFILE = "profile"  # 本机用户资料（作者名等）
    AI_SESSIONS = "ai_sessions"  # AI 助手对话记录
    SCHEDULES = "schedules"  # 定时任务（cron → 由 worker 登记 run）
    PIPELINES = "pipelines"  # 指挥台：用户保存的流水线模板
    COMPARE_BATCHES = "compare_batches"  # 回测页「策略对比」：同一冻结条件下的一组回测
    MESSAGE_FEEDBACK = "lab_message_feedback"  # Agent 消息点赞 / 点踩与意见


class WorkspaceStore:
    """按工作区隔离的 JsonStore。

    业务代码像以前一样调用 `services.store.list(...)`，这里根据**服务端已验证**的请求上下文
    把调用路由到 `<数据目录>/workspaces/<工作区 id>/store/`。没有绑定工作区时直接报错，
    而不是悄悄落到某个共享目录——这是工作区隔离的最后一道闸门。
    """

    def __init__(self, home: str | Path) -> None:
        self.home = Path(home)
        self._stores: dict[str, JsonStore] = {}
        self._lock = threading.RLock()
        self.initializer = None  # 新工作区第一次被访问时的初始化回调

    def for_workspace(self, workspace_id: str) -> JsonStore:
        if not workspace_id or not all(c.isalnum() or c in "-_" for c in workspace_id):
            raise ValueError(f"非法的工作区标识：{workspace_id!r}")
        with self._lock:
            store = self._stores.get(workspace_id)
            if store is None:
                # 结果对象存储统一放在 `<数据目录>/results` 下（不是各工作区 store 里面），
                # 路径是 `<数据目录>/results/<工作区>/<run>.<格式>`——T06 规定的落盘位置。
                store = JsonStore(
                    self.home / "workspaces" / workspace_id / "store",
                    results_root=self.home / "results",
                )
                self._stores[workspace_id] = store
                if self.initializer is not None:
                    self.initializer(workspace_id, store)
            return store

    def current(self) -> JsonStore:
        from ..platform.context import current_workspace

        return self.for_workspace(current_workspace())

    @property
    def root(self) -> Path:
        return self.current().root

    def __getattr__(self, name: str):
        return getattr(self.current(), name)
