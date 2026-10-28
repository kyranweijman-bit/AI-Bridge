"""Jupyter notebook (.ipynb) support: read a notebook as tagged cells, and
rebuild a valid notebook from edited text.

Before this, an .ipynb fell through to app.py's plain-text fallback, which
sends the notebook's raw JSON - escaped strings, metadata, and every plot as
a base64 blob. A single chart can be 50k+ characters, which on its own uses
up most of MAX_TOTAL_FILE_CHARS (60k), so the real cells after it got cut
off. Here, each cell becomes one tagged passage, like the PDF pages and
Excel row groups already are:

    [analysis.ipynb, cell 3 (code)]
    df = pd.read_csv("data.csv")
    df.head()
    --- output ---
    <text output, truncated if long>

Images and other rich outputs are replaced with a short placeholder.

The same tag format is used for "Edit my file": the model sees these
headers, returns the full file in the same shape, and
build_edited_notebook() parses the headers back into cells. Only the
standard library is used (json), so no new dependency.
"""

import json
import re
import uuid

# Per-cell cap on text output, so a cell that printed a whole DataFrame or a
# long training log doesn't crowd out everything else.
MAX_OUTPUT_CHARS = 1500

OUTPUT_DELIMITER = "--- output ---"

_CELL_HEADER = re.compile(r"^\[(?P<name>.+?), cell (?P<num>\d+) \((?P<type>code|markdown|raw)\)\]\s*$")


def _as_text(value):
    """nbformat stores multi-line strings either as one string or as a list
    of lines - normalize to one string."""
    if isinstance(value, list):
        return "".join(value)
    return value or ""


def _summarize_outputs(outputs):
    parts = []
    for out in outputs or []:
        kind = out.get("output_type")
        if kind == "stream":
            parts.append(_as_text(out.get("text")))
        elif kind == "error":
            # The traceback is full of terminal colour codes; name + message
            # is what's useful.
            parts.append(f"{out.get('ename', 'Error')}: {out.get('evalue', '')}")
        elif kind in ("execute_result", "display_data"):
            data = out.get("data", {})
            images = [m for m in data if m.startswith("image/")]
            if images:
                parts.append(f"[{images[0].split('/')[1]} image output - not sent]")
            elif "text/plain" in data:
                parts.append(_as_text(data["text/plain"]))
            elif "text/html" in data:
                parts.append("[HTML output - not sent]")
    text = "\n".join(p.rstrip() for p in parts if p and p.strip())
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n... (output truncated, {len(text):,} characters in total)"
    return text


def extract_notebook(name, raw_bytes):
    """Returns the notebook as tagged cell passages, or a short readable
    error passage if the file isn't valid notebook JSON."""
    try:
        nb = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        cells = nb["cells"]
    except Exception as e:
        return f"[{name}]\n(Could not read this notebook: {e}. Is it a valid .ipynb file?)"

    chunks = []
    for i, cell in enumerate(cells, start=1):
        cell_type = cell.get("cell_type", "code")
        if cell_type not in ("code", "markdown", "raw"):
            cell_type = "raw"
        source = _as_text(cell.get("source")).rstrip()
        output = _summarize_outputs(cell.get("outputs")) if cell_type == "code" else ""
        if not source and not output:
            continue
        chunk = f"[{name}, cell {i} ({cell_type})]\n{source}"
        if output:
            chunk += f"\n{OUTPUT_DELIMITER}\n{output}"
        chunks.append(chunk)

    if not chunks:
        return f"[{name}]\n(This notebook has no cells with content.)"
    return "\n\n".join(chunks)


def _source_lines(text):
    """Back to nbformat's list-of-lines form (each line keeps its newline,
    except the last)."""
    lines = text.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def _new_cell(cell_type, source):
    # nbformat 4.5+ requires a unique id per cell.
    cell = {"cell_type": cell_type, "id": uuid.uuid4().hex[:8], "metadata": {},
            "source": _source_lines(source)}
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def _parse_tagged_cells(text):
    """Splits edited text in the extract_notebook() format back into
    [(cell_type, source), ...]. Anything after a cell's '--- output ---'
    line is dropped - outputs are never taken from the model's text."""
    cells, current_type, current_lines = [], None, []

    def flush():
        if current_type is None:
            return
        body = "\n".join(current_lines)
        if OUTPUT_DELIMITER in body:
            body = body.split(OUTPUT_DELIMITER, 1)[0]
        cells.append((current_type, body.strip("\n")))

    for line in text.split("\n"):
        match = _CELL_HEADER.match(line.strip())
        if match:
            flush()
            current_type, current_lines = match.group("type"), []
        elif current_type is not None:
            current_lines.append(line)
    flush()
    return cells


def build_edited_notebook(text, original_bytes=None):
    """Turns the model's edited text back into .ipynb bytes.

    - If the model returned actual notebook JSON, that's used as-is.
    - Otherwise the cell headers are parsed back into cells. When the
      original notebook is available, its metadata (kernel, language) is
      kept, and any cell whose source is unchanged keeps its original
      outputs; changed or new cells get empty outputs, since the old output
      no longer matches the code.
    - If no cell headers are found at all, the whole text becomes a single
      code cell rather than failing the download.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            nb = json.loads(stripped)
            if isinstance(nb.get("cells"), list):
                return json.dumps(nb, indent=1, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass

    original = None
    if original_bytes:
        try:
            original = json.loads(original_bytes.decode("utf-8", errors="replace"))
        except Exception:
            original = None

    parsed = _parse_tagged_cells(text) or [("code", stripped)]

    # Unchanged cells keep their outputs. Matched by (type, source) rather
    # than position, so inserting or deleting a cell doesn't misalign
    # everything after it.
    # A list per key, consumed in order, so two identical cells in the
    # original each get matched once instead of sharing one object.
    original_by_source = {}
    if original:
        for cell in original.get("cells", []):
            key = (cell.get("cell_type"), _as_text(cell.get("source")).strip("\n"))
            original_by_source.setdefault(key, []).append(cell)

    cells = []
    for cell_type, source in parsed:
        matches = original_by_source.get((cell_type, source))
        cells.append(matches.pop(0) if matches else _new_cell(cell_type, source))

    nb = {
        "cells": cells,
        "metadata": (original or {}).get("metadata", {}),
        "nbformat": (original or {}).get("nbformat", 4),
        "nbformat_minor": (original or {}).get("nbformat_minor", 5),
    }
    return json.dumps(nb, indent=1, ensure_ascii=False).encode("utf-8")
