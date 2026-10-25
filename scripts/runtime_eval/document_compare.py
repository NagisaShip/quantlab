"""One document/deck planning task per engine, rendered by the same platform builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from cases import FIXTURES, parse_json
from live_compare import ROOT, Gateway, Trial, langgraph_run, legacy_class, pydantic_run, quantlab_run

BRIEF = {
    "source_id": "synthetic-research-fixture-v1",
    "audience": "research team",
    "title": "策略研究验收",
    "metrics": {"sample_days": 12, "trades": 8, "net_return_pct": 12, "max_drawdown_pct": 8, "fee_bps_per_side": 5},
    "out_of_sample": False,
    "missing": ["逐笔成交时间", "滑点假设", "基准收益"],
    "decision": "证据不足 暂不进入实盘验证",
}
PROMPT = """Read fixture doc_brief. Produce exactly one JSON object with keys markdown and slides.
markdown: a Chinese research memo of 400-650 Chinese characters with a descriptive title, conclusion first, sections 观测事实, 证据限制, 下一步验证, one Markdown table containing the supplied numeric metrics and their units, and the source ID. Preserve missing evidence and distinguish fact from hypothesis. Do not invent performance, citations, or promises.
slides: exactly 4 slide objects, each with title, bullets (3-4 short Chinese strings, at most 28 Chinese characters each), notes. The 4 slides cover decision, observed evidence, limitations, and next validation steps. Source ID in notes. Keep all supplied figures correct; make the round-trip fee explicit. No more than 4 slides including any opening slide. Return JSON only, with actual escaped newlines in markdown, no surrounding code fences."""


def build(folder, data):
    from quant_agent.local.documents import render_document
    from quant_agent.workflows.agent import _build_docx, _build_pptx, _normalize_slides

    class Profile:
        root = folder.resolve()
        home = root / ".quantlab"

        def authorize(self, path, write=False):
            target = Path(path)
            target = (target if target.is_absolute() else self.root / target).resolve()
            if not target.is_relative_to(self.root):
                raise ValueError("outside trial folder")
            return target

    profile = Profile()
    profile.home.mkdir(exist_ok=True)
    word = folder / "memo.docx"
    doc, counts = _build_docx(data["markdown"], title="", base_dir=folder, allowed_root=folder)
    doc.save(word)
    slides = _normalize_slides(data["slides"])
    deck = _build_pptx(slides, title="", base_dir=folder, allowed_root=folder)
    ppt = folder / "brief.pptx"
    deck.save(ppt)
    checks = {
        "logical_slides": len(data["slides"]),
        "physical_slides": len(deck.slides),
        "word_tables": len(doc.tables),
        "word_headings": sum(p.style.name.startswith("Heading") for p in doc.paragraphs),
        "all_notes_cite_source": all(BRIEF["source_id"] in s.get("notes", "") for s in data["slides"]),
        "source_in_memo": BRIEF["source_id"] in data["markdown"],
    }
    previews = {}
    for kind, path in [("word", word), ("slides", ppt)]:
        previews[kind] = render_document(profile, str(path), limit=8)
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (word, ppt)}
    return {"checks": checks, "previews": previews, "files": files, "word_counts": counts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    FIXTURES["doc_brief"] = BRIEF
    gateway = Gateway(os.environ["OPENCODE_API_KEY"], args.output, 40)
    legacy = legacy_class(args.output)
    results = []
    try:
        for framework in ("quantlab", "legacy", "langgraph_sqlite", "pydantic", "langgraph"):
            trial = Trial("document-" + framework)
            folder = args.output / trial.id
            folder.mkdir(exist_ok=True)
            url = gateway.url(trial)
            start = time.perf_counter()
            record = {"framework": framework, "error": None}
            try:
                task = {"prompt": PROMPT}
                if framework in {"legacy", "quantlab"}:
                    output, state = quantlab_run(trial, task, url, folder, legacy if framework == "legacy" else None)
                elif framework.startswith("langgraph"):
                    output, state = langgraph_run(trial, task, url, folder, framework == "langgraph_sqlite")
                else:
                    output, state = pydantic_run(trial, task, url)
                record.update(output=output, state=state, model_seconds=time.perf_counter() - start)
                data = parse_json(output)
                record.update(build(folder, data))
            except Exception as exc:
                record["error"] = type(exc).__name__ + ": " + str(exc)[:600]
            record["requests"] = len(trial.requests)
            record["tool_calls"] = trial.tools
            (folder / "trace.json").write_text(
                json.dumps(trial.requests, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append(record)
            (args.output / "results.json").write_text(
                json.dumps(
                    {
                        "fixture": BRIEF,
                        "prompt": PROMPT,
                        "builder_sha256": hashlib.sha256(
                            (ROOT / "src/quant_agent/workflows/agent.py").read_bytes()
                        ).hexdigest(),
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "framework": framework,
                        "requests": record["requests"],
                        "error": record["error"],
                        "checks": record.get("checks"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
