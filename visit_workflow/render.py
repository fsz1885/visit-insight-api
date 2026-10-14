from __future__ import annotations

from pathlib import Path
import re
from jinja2 import Environment, FileSystemLoader, select_autoescape, pass_context
from .models import DIMENSIONS, STAGES

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "xml"]))


def time_label(seconds):
    if seconds is None:
        return ""
    value = int(seconds)
    return f"{value // 60:02d}:{value % 60:02d}"


def evidence_label(segment):
    if segment.get("start_sec") is not None:
        label = time_label(segment["start_sec"])
        if segment.get("end_sec") is not None:
            label += "–" + time_label(segment["end_sec"])
        return label
    return f"原文第 {segment['line_start']} 行" if segment["line_start"] == segment["line_end"] else f"原文 {segment['line_start']}–{segment['line_end']} 行"


env.globals.update(evidence_label=evidence_label, stages=STAGES, dimensions=DIMENSIONS)


def readable(value):
    # References already have human-readable links; do not expose duplicate model IDs in prose.
    value = str(value)
    value = re.sub(r"[（(]\s*E\d{5}(?:[\s/、,，\-–—]+E\d{5})*\s*[)）]", "", value)
    value = re.sub(r"(?<![A-Za-z0-9])E\d{5}(?:[\s/、,，\-–—]+E\d{5})*\s*", "", value)
    return value


env.filters["readable"] = readable


@pass_context
def source_locations(context, value):
    evidence = context.get("report", {}).get("evidence", {})
    return re.sub(r"E\d{5}", lambda m: evidence_label(evidence[m.group()]) if m.group() in evidence else m.group(), str(value))


env.filters["source_locations"] = source_locations


def render_report(report: dict, destination: Path):
    content = env.get_template("report.html").render(report=report,
        css=(TEMPLATES / "report.css").read_text(encoding="utf-8"),
        script=(TEMPLATES / "report.js").read_text(encoding="utf-8"))
    destination.write_text(content, encoding="utf-8")
