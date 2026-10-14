"""Parse text without inventing speakers, dates or timestamps."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree
from .models import Segment, Meta

ALLOWED = {".txt", ".md", ".srt", ".vtt", ".json", ".docx"}
MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 180_000
TIME = r"\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
STAMP = re.compile(rf"[\[【（(]?({TIME})(?:\s*(?:-->|[-–—~至])\s*({TIME}))?[\]】）)]?")
SPEAKER = re.compile(r"^([^:：\n]{1,32})[:：]\s*(.*)$")


def resolve_visit_date(meta: Meta, filename: str) -> Meta:
    """The user defines the trailing filename date as the visit date."""
    if meta.visit_date:
        return meta
    stem = Path(filename.replace("\\", "/")).stem
    match = re.search(r"(?<!\d)(\d{4})([-_]?)(\d{2})\2(\d{2})$", stem)
    if match:
        try:
            value = date(int(match[1]), int(match[3]), int(match[4])).isoformat()
            return meta.model_copy(update={"visit_date": value})
        except ValueError:
            pass
    return meta


def is_speaker_label(label: str) -> bool:
    if re.fullmatch(r"(?:说话人|讲话人|speaker|客户|销售|顾问)\s*\d*", label, re.I):
        return True
    return bool(label) and len(label) <= 16 and not re.search(r"[\d，。！？；、,.!?;]", label)


def seconds(value: str | int | float | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        parts = str(value).replace(",", ".").split(":")
        if not 1 <= len(parts) <= 3:
            raise ValueError("时间戳格式不正确")
        vals = [float(p) for p in parts]
        if any(v < 0 for v in vals) or (len(vals) > 1 and any(v >= 60 for v in vals[1:])):
            raise ValueError("时间戳的秒 / 分钟超出范围")
        n = sum(v * (60 ** i) for i, v in enumerate(reversed(vals)))
    import math
    if not math.isfinite(n) or n < 0:
        raise ValueError("时间戳必须是有效的非负数")
    return n


def decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法识别文本编码，请另存为 UTF-8 的 TXT 文件")
    if "\x00" in text:
        raise ValueError("文件包含二进制数据，请使用纯文本或 DOCX 文件")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_file(data: bytes, filename: str) -> tuple[str, list[Segment], list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("支持 TXT、MD、SRT、VTT、JSON 和 DOCX 转写文本；不接收录音或报告 PDF")
    if len(data) > MAX_BYTES:
        raise ValueError("文件超过 2 MB，请按单次接访拆分")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                info = z.getinfo("word/document.xml")
                if info.file_size > 12 * 1024 * 1024:
                    raise ValueError("DOCX 正文过大")
                root = ElementTree.fromstring(z.read(info))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise ValueError("DOCX 文件损坏或不含可读正文") from exc
    else:
        text = decode(data)
    if not text.strip():
        raise ValueError("文本为空")
    if len(text) > MAX_CHARS:
        raise ValueError("单次文本超过 18 万字符，请按接访拆分；系统不会截断原文")
    if suffix == ".json":
        segments = parse_json(text)
    else:
        segments = parse_lines(text, subtitle=suffix in {".srt", ".vtt"})
    if not segments:
        raise ValueError("未找到可分析的对话正文")
    warnings = []
    if not any(s.start_sec is not None for s in segments):
        warnings.append("原文没有时间戳，证据使用原文行号；无法确认录音时长。")
    if all(s.speaker == "未标注" for s in segments):
        warnings.append("原文没有明确说话人标签，角色归属需要结合上下文复核。")
    if any(s.end_sec is not None and s.start_sec is not None and s.end_sec < s.start_sec for s in segments):
        raise ValueError("存在结束时间早于开始时间的片段")
    return text, segments, warnings


def parse_json(text: str) -> list[Segment]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 格式不正确") from exc
    rows = obj.get("segments", obj.get("utterances")) if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        raise ValueError("JSON 需要 segments / utterances 数组或顶层片段数组")
    segments = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError("JSON 中每个片段必须是对象")
        raw = row.get("text", row.get("content", ""))
        if not isinstance(raw, str):
            raise ValueError("片段 text 必须是字符串")
        if not raw.strip():
            continue
        start = seconds(row.get("start_sec", row.get("start")))
        end = seconds(row.get("end_sec", row.get("end")))
        if end is not None and start is None:
            raise ValueError("片段有结束时间却没有开始时间")
        segments.append(Segment(id=f"E{len(segments)+1:05d}", text=raw.strip(),
                                speaker=str(row.get("speaker", row.get("speaker_id", "未标注"))),
                                start_sec=start, end_sec=end, line_start=i, line_end=i))
    return segments


def parse_lines(text: str, subtitle: bool = False) -> list[Segment]:
    segments: list[Segment] = []
    pending_start = pending_end = None
    pending_speaker = "未标注"
    for line_no, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw:
            if subtitle:
                pending_start = pending_end = None
                pending_speaker = "未标注"
            continue
        if subtitle and (raw.isdigit() or raw.startswith(("WEBVTT", "NOTE", "Kind:", "Language:"))):
            continue
        stamp = STAMP.search(raw)
        # Only use timestamp prefixes, or timestamps immediately after a short speaker label.
        prefix = raw[:stamp.start()].strip(" []【】():：") if stamp else ""
        has_stamp = stamp is not None and (stamp.start() <= 1 or (is_speaker_label(prefix) and (raw[stamp.start()-1].isspace() or re.fullmatch(r"客户|销售|顾问|说话人\s*\d+|讲话人\s*\d+", prefix))))
        start = end = None
        speaker = "未标注"
        content = raw
        if has_stamp:
            start, end = seconds(stamp.group(1)), seconds(stamp.group(2))
            content = raw[stamp.end():].strip(" \t:-：")
            if prefix:
                speaker = prefix
            if subtitle:
                content = re.sub(r"^(?:align|position|line|size):.*$", "", content)
            pending_start, pending_end = start, end
        match = SPEAKER.match(content)
        if match:
            label = match.group(1).strip(" []【】")
            if is_speaker_label(label) and not re.match(r"^https?$", label, re.I):
                speaker, content = label, match.group(2).strip()
        elif re.fullmatch(r"(?:说话人|讲话人|speaker)\s*\d+|客户|销售|顾问|置业顾问", content, re.I):
            speaker, content = content, ""
        if speaker != "未标注":
            pending_speaker = speaker
        if not content:
            continue
        if not has_stamp:
            start, end = pending_start, pending_end
        if speaker == "未标注":
            speaker = pending_speaker
        # Long paragraphs are split without losing text or changing source line numbers.
        for offset in range(0, len(content), 3000):
            segments.append(Segment(id=f"E{len(segments)+1:05d}", speaker=speaker,
                text=content[offset:offset+3000], start_sec=start, end_sec=end,
                line_start=line_no, line_end=line_no))
    return segments


def chunk_segments(segments: list[Segment], max_chars: int = 16000) -> list[list[Segment]]:
    chunks, current, count = [], [], 0
    for seg in segments:
        size = len(seg.text) + 100
        if current and count + size > max_chars:
            chunks.append(current)
            current = current[-2:]
            count = sum(len(s.text)+100 for s in current)
        current.append(seg)
        count += size
    if current:
        chunks.append(current)
    return chunks
