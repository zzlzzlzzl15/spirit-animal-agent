"""文档提取工具 — read_document（从 ipynb/docx/xlsx 提取文本）。

参考 Hermes 的 tools/read_extract.py 设计：
- 支持 Jupyter Notebook (.ipynb)
- 支持 Word 文档 (.docx)
- 支持 Excel 表格 (.xlsx)
- 纯标准库实现，无额外依赖
"""

import json
import logging
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".ipynb", ".docx", ".xlsx"}


# ---------------------------------------------------------------------------
# 提取逻辑
# ---------------------------------------------------------------------------

def _extract_notebook(path: str) -> str:
    """提取 Jupyter Notebook 内容。"""
    import json as _json

    with open(path, encoding="utf-8", errors="replace") as f:
        nb = _json.load(f)

    if not isinstance(nb, dict):
        raise ValueError("不是有效的 Notebook 文件")

    cells = nb.get("cells", [])
    if not cells:
        raise ValueError("Notebook 没有 cell")

    output = []
    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_type = cell.get("cell_type", "")
        if cell_type not in labels:
            continue

        counts[cell_type] += 1
        suffix = f" {counts[cell_type]}" if cell_type != "raw" else ""

        source = cell.get("source", [])
        if isinstance(source, list):
            source_text = "".join(s for s in source if isinstance(s, str))
        else:
            source_text = str(source)

        output.append(f"# ── {labels[cell_type]} cell{suffix} ──")
        output.append(source_text.rstrip("\n"))
        output.append("")

        # 如果有输出（code cell）
        if cell_type == "code" and "outputs" in cell:
            for out in cell.get("outputs", []):
                if isinstance(out, dict):
                    text = out.get("text", [])
                    if isinstance(text, list):
                        text = "".join(str(t) for t in text)
                    if text and text.strip():
                        output.append(f"  # Output: {text.strip()[:200]}")

    if not output:
        raise ValueError("Notebook 没有可读内容")

    return "\n".join(output).rstrip("\n")


def _extract_docx(path: str) -> str:
    """提取 Word 文档文本（纯标准库）。"""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    with zipfile.ZipFile(path) as zf:
        with zf.open("word/document.xml") as doc_xml:
            tree = ET.parse(doc_xml)

    root = tree.getroot()
    paragraphs = []

    for para in root.iter("{%s}p" % ns["w"]):
        texts = []
        for run in para.iter("{%s}t" % ns["w"]):
            if run.text:
                texts.append(run.text)
        if texts:
            paragraphs.append("".join(texts))

    return "\n\n".join(paragraphs)


def _extract_xlsx(path: str) -> str:
    """提取 Excel 表格内容（纯标准库）。"""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    with zipfile.ZipFile(path) as zf:
        # 读取共享字符串
        shared_strings = []
        try:
            with zf.open("xl/sharedStrings.xml") as ss_xml:
                tree = ET.parse(ss_xml)
                for si in tree.getroot().iter("{%s}si" % ns["s"]):
                    texts = []
                    for t in si.iter("{%s}t" % ns["s"]):
                        if t.text:
                            texts.append(t.text)
                    shared_strings.append("".join(texts))
        except KeyError:
            pass

        # 读取第一个 sheet
        try:
            with zf.open("xl/worksheets/sheet1.xml") as sheet_xml:
                tree = ET.parse(sheet_xml)
        except KeyError:
            return "(空工作簿)"

    rows = []
    for row in tree.getroot().iter("{%s}row" % ns["s"]):
        cells = []
        for cell in row.iter("{%s}c" % ns["s"]):
            value = ""
            cell_type = cell.get("t", "")
            v_elem = cell.find("{%s}v" % ns["s"])

            if v_elem is not None and v_elem.text:
                if cell_type == "s":
                    idx = int(v_elem.text)
                    value = shared_strings[idx] if idx < len(shared_strings) else v_elem.text
                else:
                    value = v_elem.text

            cells.append(value)

        if any(cells):
            rows.append(" | ".join(cells))

    return "\n".join(rows[:5000])  # 限制行数


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

READ_DOCUMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_document",
        "description": (
            "从文档文件中提取文本内容。\n\n"
            "支持格式：\n"
            "- .ipynb — Jupyter Notebook\n"
            "- .docx — Word 文档\n"
            "- .xlsx — Excel 表格\n\n"
            "对于纯文本文件，请使用 read_file。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文档文件路径",
                },
            },
            "required": ["path"],
        },
    },
}


def _read_document_impl(path: str) -> str:
    """读取文档文件。"""
    target = Path(path)

    if not target.exists():
        return json.dumps({"error": f"文件不存在: {path}"})

    ext = target.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return json.dumps({
            "error": f"不支持的格式: {ext}",
            "supported": list(SUPPORTED_EXTENSIONS),
        })

    try:
        if ext == ".ipynb":
            text = _extract_notebook(str(target))
        elif ext == ".docx":
            text = _extract_docx(str(target))
        elif ext == ".xlsx":
            text = _extract_xlsx(str(target))
        else:
            return json.dumps({"error": f"未实现的格式: {ext}"})

        # 截断
        from spirit.config import get_config_value
        max_chars = get_config_value("limits.read_extract_max_chars", 30000)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n\n... [内容截断，共 {len(text)} 字符]"

        return json.dumps({
            "path": str(target),
            "format": ext,
            "content": text,
            "truncated": truncated,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"提取失败: {e}"})


registry.register(
    name="read_document",
    toolset="file",
    schema=READ_DOCUMENT_SCHEMA,
    handler=_read_document_impl,
    description="读取文档文件（ipynb/docx/xlsx）",
    emoji="📄",
    max_result_size_chars=30000,
)
