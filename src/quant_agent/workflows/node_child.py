"""Standalone process protocol. A process boundary is NOT a hostile-code sandbox."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback
from pathlib import Path


class Context:
    def __init__(self, request):
        self.files = Path(request["files"]).resolve()
        self.work = Path(request["work"]).resolve()
        self.seed = request.get("seed", 7)
        self.logs = []
        self.input_files = request.get("input_files", {})

    def file(self, name):
        if name in self.input_files:
            p = Path(self.input_files[name]).resolve()
            if not p.is_file():
                raise ValueError("本次已核验的引用文件缺失")
            return p
        p = (self.files / name).resolve()
        if not p.is_relative_to(self.files) or not p.is_file():
            raise ValueError("输入文件不在本工作区的已导入文件中")
        return p

    def log(self, message):
        if len(self.logs) < 100:
            self.logs.append(str(message)[:2000])

    def save_file(self, source):
        """Materialize immutable binary outputs; downstream receives a tiny ref."""
        import hashlib
        import os
        import shutil
        import uuid

        source = Path(source).resolve()
        if not source.is_relative_to(Path(self.work).resolve()) or not source.is_file():
            raise ValueError("仅能发布本次临时目录内的输出文件")
        h = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        directory = self.files / "generated"
        directory.mkdir(exist_ok=True)
        target = directory / (digest + source.suffix)
        if not target.exists():
            temporary = target.with_name("." + uuid.uuid4().hex[:12] + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        return {"file": target.relative_to(self.files).as_posix(), "sha256": digest, "bytes": target.stat().st_size}


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result_path = Path(sys.argv[2])
    capture = io.StringIO()
    ctx = Context(request)
    for directory in request.get("dependency_paths", []):
        if directory not in sys.path:
            sys.path.append(directory)
    try:
        namespace = {"__name__": "workflow_card"}
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            exec(compile(request["code"], "<workflow-card>", "exec"), namespace)
            result = namespace["execute"](ctx, request["inputs"], request["params"])
        payload = {
            "ok": True,
            "outputs": result,
            "logs": ctx.logs,
            "stdout": capture.getvalue()[-16000:],
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except BaseException as exc:
        encoded = json.dumps(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
                "logs": ctx.logs,
                "stdout": capture.getvalue()[-16000:],
            },
            ensure_ascii=False,
        )
    result_path.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
