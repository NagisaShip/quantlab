"""对话里的外链图片：第一次显示时由服务端下载并缓存到工作区，之后都从本地读取。

为什么要代理而不是让浏览器直接加载：
- Agent 引用的图片链接常常会过期或防盗链，下载一份留在工作区，历史对话里的图才不会变成裂图；
- 浏览器不必直接访问第三方站点（不泄露研究者的 IP 与 Referer）。

安全：只接受公网 http(s) 地址（每一跳跳转都重新检查，防 SSRF），只接受图片类型，单张不超过 8 MB。
"""

from __future__ import annotations

import hashlib
import json

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .deps import get_services

router = APIRouter(prefix="/api/media", tags=["对话媒体"])

MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/avif", "image/bmp")


@router.get("/image", summary="下载并缓存一张外链图片（对话里的图片自动下载）")
def image(url: str = Query(..., min_length=8, max_length=2000)) -> Response:
    s = get_services()
    folder = s.store.root / "media"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    meta_path, data_path = folder / f"{key}.json", folder / f"{key}.bin"
    if meta_path.exists() and data_path.exists():
        ctype = json.loads(meta_path.read_text(encoding="utf-8")).get("content_type", "image/png")
        return _respond(data_path.read_bytes(), ctype)
    data, ctype = _download(s.websearch, url)
    folder.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(data)
    meta_path.write_text(json.dumps({"url": url, "content_type": ctype, "bytes": len(data)}), encoding="utf-8")
    return _respond(data, ctype)


def _respond(data: bytes, ctype: str) -> Response:
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if ctype == "image/svg+xml":  # SVG 里可能带脚本：只当图片显示，禁止当文档执行
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return Response(content=data, media_type=ctype, headers=headers)


def _download(websearch, url: str) -> tuple[bytes, str]:
    from ..llm.websearch import WebSearchError

    current = url
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            for _ in range(4):
                websearch._check_url(current)
                with client.stream(
                    "GET", current, headers={"User-Agent": "QuantLab-Media/1.0", "Accept": "image/*"}
                ) as res:
                    if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
                        current = str(httpx.URL(current).join(res.headers["location"]))
                        continue
                    if res.status_code >= 400:
                        raise HTTPException(502, f"图片地址返回 {res.status_code}")
                    ctype = res.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in IMAGE_TYPES:
                        raise HTTPException(415, f"不是图片：{ctype or '未知类型'}")
                    buf = bytearray()
                    for chunk in res.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise HTTPException(413, "图片超过 8 MB")
                    return bytes(buf), ctype
    except WebSearchError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"图片下载失败：{exc.__class__.__name__}") from exc
    raise HTTPException(502, "跳转次数过多")
