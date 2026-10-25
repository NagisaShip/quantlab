"""Fixed-data chart export and real-page rendering; no model calls or notifications."""

import argparse
import json
from pathlib import Path

from quant_agent.local.documents import render_document
from quant_agent.workflows.agent import _build_docx, _build_pptx, _normalize_slides

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.output.resolve()
root.mkdir(parents=True, exist_ok=True)
charts = [
    {
        "kind": "bar",
        "title": "两组实验的收益对比",
        "labels": ["策略甲", "策略乙", "策略丙"],
        "series": [{"name": "样本内", "values": [-2.5, 4, 6]}, {"name": "样本外", "values": [-1, 2, 3]}],
        "unit": "%",
        "source": "固定测试数据 非真实业绩",
    },
    {
        "kind": "line",
        "title": "样本期间净值变化",
        "labels": ["第一期", "第二期", "第三期", "第四期"],
        "series": [{"name": "策略甲", "values": [100, 98, 103, 105]}, {"name": "基准", "values": [100, 101, 102, 103]}],
        "unit": "元",
        "source": "固定测试数据 非真实业绩",
    },
    {
        "kind": "pie",
        "title": "测试组合的资产构成",
        "labels": ["现金", "股票", "债券"],
        "series": [{"name": "比例", "values": [25, 45, 30]}],
        "unit": "%",
        "source": "固定测试数据 非真实持仓",
    },
]
markdown = "本报告验证图表进入 Word 和 PPT 后的数据、单位和来源是否保留。所有数值均为固定测试数据。\n\n"
for chart in charts:
    markdown += "## " + chart["title"] + "\n\n```chart\n" + json.dumps(chart, ensure_ascii=False) + "\n```\n\n"
doc, counts = _build_docx(markdown, title="图表交付验收", base_dir=root, allowed_root=root)
doc.save(root / "charts.docx")
slides = [{"title": "图表交付验收", "bullets": ["三类图表保留数值、单位和来源", "固定测试数据，不代表真实业绩"]}] + [
    {"title": c["title"], "chart": c, "bullets": [], "notes": c["source"]} for c in charts
]
ppt = _build_pptx(_normalize_slides(slides), title="", base_dir=root, allowed_root=root, include_cover=False)
ppt.save(root / "charts.pptx")


class Profile:
    home = root / ".quantlab"

    def authorize(self, path, write=False):
        p = Path(path).resolve()
        if not p.is_relative_to(root):
            raise ValueError("outside QA root")
        return p


profile = Profile()
profile.root = root
results = {}
for suffix in ["docx", "pptx"]:
    results[suffix] = render_document(profile, str(root / ("charts." + suffix)), limit=8)
(root / "checks.json").write_text(
    json.dumps({"counts": counts, "slides": len(ppt.slides), "previews": results}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(
    json.dumps(
        {k: {"pages": v["total_pages"], "images": [p["path"] for p in v["pages"]]} for k, v in results.items()},
        ensure_ascii=False,
    )
)
