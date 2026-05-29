#!/usr/bin/env python3
"""
MarkItDown Attachments — a *token-free* MCP server (with OCR).

Converts Claude chat / project attachments (and any local files) to Markdown
using Microsoft's `markitdown` library. Conversion runs entirely locally; the
resulting Markdown is WRITTEN TO DISK as `.md` files and only compact metadata
(file paths, byte/char counts, status) is returned to the model — so converting
attachments costs effectively zero context tokens.

Beyond the stock `markitdown-mcp` this server adds:
  • OCR (Tesseract) for images and scanned/image-only PDFs, with automatic
    orientation detection. `ocr`: "auto" (default), "off", "force", or "hybrid"
    (per-page: keep embedded text, OCR only the sparse/image pages — ideal for
    mixed image-heavy PDFs).
  • Existing `.md`/`.markdown` files are carried through into the output set
    (copied, structure-preserved) so a folder sweep yields a COMPLETE collection.
  • Google Drive pointer files (.gdoc/.gslides/.gsheet/.gdrive) become clickable
    "Open in Drive" link notes (no raw JSON / no embedded email).
  • Collision-safe output names: `name.pdf.md` / `name.docx.md` (no clobber).
  • Structure-preserving output, rich token-free summary, and `detail="summary"`
    to shrink the tool result for very large sweeps (full manifest still on disk).

Tools: convert_attachments_to_markdown, list_convertible_attachments,
       convert_one, peek_markdown, ocr_capabilities.

Config env (also settable via DXT user_config / .mcp.json env):
  MARKITDOWN_INPUT_DIR, MARKITDOWN_OUTPUT_DIR, MARKITDOWN_ENABLE_PLUGINS,
  MARKITDOWN_OCR (auto/off/force/hybrid), MARKITDOWN_OCR_LANG (e.g. eng+ben),
  MARKITDOWN_OCR_MAX_PAGES, TESSERACT_CMD.
"""

import glob as _glob
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP

# Ensure Homebrew/local bin dirs are on PATH so spawned helpers resolve (ffmpeg
# for audio; tesseract is also found via absolute path) even when the host app
# launches this server with a sparse PATH.
for _d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

mcp = FastMCP("markitdown-attachments")

CONVERTIBLE_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm",
    ".html", ".htm", ".csv", ".tsv", ".json", ".xml", ".rss", ".atom",
    ".epub", ".zip", ".msg", ".txt", ".rtf", ".ipynb",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".wav", ".mp3", ".m4a", ".flac",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
POINTER_EXTS = {".gdoc", ".gslides", ".gsheet", ".gdrive"}
MARKDOWN_EXTS = {".md", ".markdown"}   # carried through (copied), not re-converted
GATHER_EXTS = CONVERTIBLE_EXTS | POINTER_EXTS

OCR_DPI = 200                 # rasterization DPI for scanned-PDF OCR
HYBRID_PAGE_MIN_CHARS = 80    # in hybrid mode, pages with less text than this get OCR'd

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _plugins_enabled() -> bool:
    return os.getenv("MARKITDOWN_ENABLE_PLUGINS", "false").strip().lower() in ("true", "1", "yes")


_MD = None
def _md() -> MarkItDown:
    global _MD
    if _MD is None:
        _MD = MarkItDown(enable_plugins=_plugins_enabled())
    return _MD


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def _env_dir(var: str) -> Optional[Path]:
    v = os.getenv(var)
    return _expand(v) if v else None


def _glob_matches(pattern: str) -> list:
    return [Path(m) for m in _glob.glob(os.path.expanduser(os.path.expandvars(pattern)), recursive=True)]


def _ocr_mode(v: Optional[str]) -> str:
    v = (v or os.getenv("MARKITDOWN_OCR") or "auto").strip().lower()
    return v if v in ("auto", "off", "force", "hybrid") else "auto"


def _ocr_lang(v: Optional[str]) -> str:
    return (v or os.getenv("MARKITDOWN_OCR_LANG") or "eng").strip()


def _ocr_max_pages(v: Optional[int]) -> int:
    if v is None:
        try:
            v = int(os.getenv("MARKITDOWN_OCR_MAX_PAGES", "50"))
        except ValueError:
            v = 50
    return max(1, int(v))


def _is_pointer(path: Path) -> bool:
    return path.suffix.lower() in POINTER_EXTS


def _is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_EXTS


# --------------------------------------------------------------------------- #
# OCR (Tesseract via subprocess + stdin pipe — no temp files, robust decoding)
# --------------------------------------------------------------------------- #

_TESS_CMD = None
_TESS_OK = None

def _tess_cmd() -> str:
    global _TESS_CMD
    if _TESS_CMD is None:
        cand = (os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
                or next((p for p in ("/opt/homebrew/bin/tesseract",
                                      "/usr/local/bin/tesseract",
                                      "/usr/bin/tesseract") if os.path.exists(p)), "tesseract"))
        _TESS_CMD = cand
    return _TESS_CMD


def _tesseract_ok() -> bool:
    global _TESS_OK
    if _TESS_OK is None:
        try:
            _TESS_OK = subprocess.run([_tess_cmd(), "--version"], capture_output=True).returncode == 0
        except Exception:
            _TESS_OK = False
    return _TESS_OK


def _ocr_pil(pil, lang: str, psm: int = 1) -> str:
    """OCR a PIL image. PSM 1 = auto page segmentation WITH orientation detection."""
    buf = io.BytesIO()
    pil.convert("RGB").save(buf, format="PNG")
    r = subprocess.run([_tess_cmd(), "stdin", "stdout", "-l", lang, "--psm", str(psm)],
                       input=buf.getvalue(), capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def _ocr_image(path: Path, lang: str) -> str:
    from PIL import Image
    with Image.open(path) as im:
        return _ocr_pil(im, lang)


def _ocr_pdf(path: Path, lang: str, max_pages: int):
    """OCR every page (rasterize). Returns (text, total_pages, pages_done, truncated)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        n = len(pdf)
        done = min(n, max_pages)
        parts = []
        for i in range(done):
            page = pdf[i]
            pil = page.render(scale=OCR_DPI / 72).to_pil()
            page.close()
            t = _ocr_pil(pil, lang).strip()
            if t:
                parts.append(f"<!-- page {i + 1} (OCR) -->\n\n{t}")
        return "\n\n".join(parts), n, done, n > done
    finally:
        pdf.close()


def _hybrid_pdf(path: Path, lang: str, max_pages: int):
    """Per-page: keep embedded text; OCR only sparse/image pages.
    Returns (text, total_pages, pages_done, truncated, ocr_pages)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        n = len(pdf)
        done = min(n, max_pages)
        parts = []
        ocr_pages = 0
        for i in range(done):
            page = pdf[i]
            tp = page.get_textpage()
            txt = (tp.get_text_range() or "").strip()
            tp.close()
            used = False
            if len(txt) < HYBRID_PAGE_MIN_CHARS:
                o = _ocr_pil(page.render(scale=OCR_DPI / 72).to_pil(), lang).strip()
                if len(o) > len(txt):
                    txt, used = o, True
                    ocr_pages += 1
            page.close()
            if txt:
                parts.append(f"<!-- page {i + 1}{' (OCR)' if used else ''} -->\n\n{txt}")
        return "\n\n".join(parts), n, done, n > done, ocr_pages
    finally:
        pdf.close()


# --------------------------------------------------------------------------- #
# Google Drive pointer stubs
# --------------------------------------------------------------------------- #

def _pointer_markdown(path: Path) -> str:
    try:
        d = json.loads(path.read_text(errors="replace"))
    except Exception:
        d = {}
    doc_id = d.get("doc_id", "")
    kind, prefix = {
        ".gsheet": ("spreadsheet", "https://docs.google.com/spreadsheets/d/"),
        ".gslides": ("presentation", "https://docs.google.com/presentation/d/"),
        ".gdoc": ("document", "https://docs.google.com/document/d/"),
        ".gdrive": ("file", "https://drive.google.com/file/d/"),
    }.get(path.suffix.lower(), ("file", "https://drive.google.com/file/d/"))
    url = (prefix + doc_id) if doc_id else "(unknown — open from Google Drive)"
    return (f"# {path.stem}\n\n"
            f"> **Google Drive {kind} pointer** — this file holds no local content; "
            f"the document lives in Google Drive.\n\n"
            f"[Open in Google Drive]({url})\n")


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #

def _ocr_needed(ext: str, base_len: int, mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "force":
        return ext in IMAGE_EXTS or ext == ".pdf"
    if ext in IMAGE_EXTS:      # auto
        return base_len < 1
    if ext == ".pdf":
        return base_len < 50
    return False


def _convert_file(path: Path, ocr: str, lang: str, max_pages: int):
    """Convert one file to markdown text. Returns (text, meta)."""
    ext = path.suffix.lower()
    meta = {"method": "markitdown", "ocr_used": False, "is_image": ext in IMAGE_EXTS,
            "pages": None, "ocr_truncated": False}

    if _is_pointer(path):
        meta["method"] = "drive-link"
        return _pointer_markdown(path), meta

    # Hybrid PDF: per-page text-or-OCR (full coverage, no redundant OCR of text pages).
    if ocr == "hybrid" and ext == ".pdf":
        try:
            text, n, _done, trunc, ocr_pages = _hybrid_pdf(path, lang, max_pages)
            meta["pages"] = n
            meta["ocr_truncated"] = trunc
            if ocr_pages:
                meta["ocr_used"] = True
                meta["ocr_pages"] = ocr_pages
                meta["method"] = "hybrid (text+OCR)"
            else:
                meta["method"] = "pdf-text"
            return text, meta
        except Exception as e:  # noqa: BLE001 — fall back to plain markitdown
            meta["ocr_error"] = f"{type(e).__name__}: {e}"

    base = ""
    try:
        base = (_md().convert(str(path)).markdown or "").strip()
    except Exception as e:  # noqa: BLE001
        meta["convert_error"] = f"{type(e).__name__}: {e}"

    text = base
    eff = "force" if (ocr == "hybrid" and ext in IMAGE_EXTS) else ("auto" if ocr == "hybrid" else ocr)
    if _ocr_needed(ext, len(base), eff):
        if not _tesseract_ok():
            meta["ocr_status"] = "unavailable (install tesseract for image/scanned-PDF OCR)"
        else:
            try:
                if ext in IMAGE_EXTS:
                    extra = _ocr_image(path, lang).strip()
                elif ext == ".pdf":
                    extra, n, _done, trunc = _ocr_pdf(path, lang, max_pages)
                    extra = extra.strip()
                    meta["pages"] = n
                    meta["ocr_truncated"] = trunc
                else:
                    extra = ""
                if extra:
                    text = (base + "\n\n" if base else "") + extra
                    meta["method"] = "markitdown+ocr" if base else "ocr"
                    meta["ocr_used"] = True
            except Exception as e:  # noqa: BLE001
                meta["ocr_error"] = f"{type(e).__name__}: {e}"
    return text, meta


def _empty_reason(path: Path, meta: dict) -> str:
    if meta.get("convert_error"):
        return f"conversion error: {meta['convert_error']}"
    if meta.get("ocr_error"):
        return f"no text; OCR error: {meta['ocr_error']}"
    if path.suffix.lower() in IMAGE_EXTS and not _tesseract_ok():
        return "image with no embedded text — install tesseract to OCR it"
    if path.suffix.lower() in IMAGE_EXTS:
        return "image — OCR found no readable text"
    return "no extractable text (may be scanned; retry with ocr='force' or 'hybrid')"


# --------------------------------------------------------------------------- #
# Gathering inputs + choosing output paths
# --------------------------------------------------------------------------- #

def _gather(sources, input_dir, recursive, include_markdown=False):
    """Ordered, de-duplicated [(file, base_dir|None)]. base_dir enables
    structure-preserving output for directory scans. Markdown files are included
    (for carry-through copy) only when include_markdown is True."""
    gather_exts = GATHER_EXTS | (MARKDOWN_EXTS if include_markdown else set())
    items = []
    seen = set()

    def add(path: Path, base):
        rp = path.resolve()
        if rp in seen or rp.suffix.lower() not in gather_exts:
            return
        seen.add(rp)
        items.append((rp, base))

    def add_dir(d: Path):
        it = d.rglob("*") if recursive else d.glob("*")
        for f in sorted(it):
            if f.is_file() and f.suffix.lower() in gather_exts:
                add(f, d)

    if sources:
        for s in sources:
            matches = _glob_matches(s)
            for c in (matches if matches else [_expand(s)]):
                if c.is_dir():
                    add_dir(c)
                elif c.is_file():
                    add(c, None)
    else:
        d = _expand(input_dir) if input_dir else _env_dir("MARKITDOWN_INPUT_DIR")
        if d and d.is_dir():
            add_dir(d)
    return items


def _pick_target(path: Path, base, out_dir, preserve, used: set) -> Path:
    """Collision-free .md path. Same-stem different-type files become
    `name.pdf.md` / `name.docx.md` rather than clobbering or opaque `-2`."""
    if out_dir is None:
        folder = path.parent
    elif preserve and base is not None:
        try:
            rel = path.relative_to(base).parent
        except ValueError:
            rel = Path()
        folder = out_dir / rel
    else:
        folder = out_dir
    cand = folder / f"{path.stem}.md"
    if cand in used:
        cand = folder / f"{path.stem}{path.suffix}.md"
    k = 2
    while cand in used:
        cand = folder / f"{path.stem}{path.suffix}-{k}.md"
        k += 1
    return cand


def _write_index(out_dir: Path, entries: list) -> Path:
    lines = ["# Converted attachments — index", ""]
    for c in sorted(entries, key=lambda r: r["markdown_file"]):
        mp = Path(c["markdown_file"])
        try:
            rel = mp.relative_to(out_dir)
        except ValueError:
            rel = mp
        tags = []
        if c.get("ocr"):
            tags.append("OCR")
        if c.get("method") == "copied-markdown":
            tags.append("copied")
        tag = (" · " + ", ".join(tags)) if tags else ""
        lines.append(f"- [{Path(c['source']).name}]({rel}) — {c.get('chars', 0)} chars{tag}")
    idx = out_dir / "INDEX.md"
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return idx


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def convert_attachments_to_markdown(
    sources: Optional[list] = None,
    input_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    recursive: bool = True,
    overwrite: bool = False,
    ocr: Optional[str] = None,
    ocr_lang: Optional[str] = None,
    ocr_max_pages: Optional[int] = None,
    preserve_structure: bool = True,
    write_index: bool = False,
    include_existing_markdown: bool = True,
    detail: str = "full",
) -> dict:
    """Convert attachments/files to Markdown FILES ON DISK without returning their content.

    Token-free: converted Markdown is written to `.md` files; only compact metadata
    (paths, byte/char counts, status) is returned — never the document text.

    Args:
      sources: explicit file paths, directories, and/or glob patterns. Directories are
               scanned. If omitted, falls back to `input_dir` (or MARKITDOWN_INPUT_DIR).
      input_dir: directory to scan when `sources` is not provided.
      output_dir: where to write `.md` files. If omitted, uses MARKITDOWN_OUTPUT_DIR,
                  otherwise writes each `.md` next to its source file.
      recursive: recurse into sub-directories when scanning (default True).
      overwrite: overwrite an existing `.md` target (default False -> skipped).
      ocr: "auto" (default; OCR images & image-only PDFs), "off", "force" (OCR every
           image/PDF), or "hybrid" (per-page: keep text pages, OCR only image pages —
           best for mixed image-heavy PDFs). Requires Tesseract; degrades gracefully.
      ocr_lang: Tesseract language code(s), e.g. "eng" or "eng+ben".
      ocr_max_pages: cap on PDF pages OCR'd per file (default 50; truncation reported).
      preserve_structure: mirror source sub-folders under output_dir (default True).
      write_index: also write an INDEX.md linking every output file (output_dir only).
      include_existing_markdown: copy existing .md/.markdown inputs into the output set
           so a folder sweep yields a complete collection (default True).
      detail: "full" (default; per-file arrays) or "summary" (omit the big `converted`
           list to save tokens — totals + failures + a small sample; full list in INDEX.md).

    Returns: {output_root, summary, totals, ocr_available, converted[]?, markdown_copied[]?,
              empty[], skipped[], failed[], index_file?}
    """
    out_dir = _expand(output_dir) if output_dir else _env_dir("MARKITDOWN_OUTPUT_DIR")
    mode = _ocr_mode(ocr)
    lang = _ocr_lang(ocr_lang)
    maxp = _ocr_max_pages(ocr_max_pages)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    items = _gather(sources, input_dir, recursive, include_markdown=include_existing_markdown)
    converted, md_copied, empty, skipped, failed = [], [], [], [], []
    used: set = set()
    ocr_count = pointers = total_chars = total_bytes = 0

    if sources:
        for s in sources:
            if not _expand(s).exists() and not _glob_matches(s):
                failed.append({"source": s, "error": "not found"})

    for path, base in items:
        try:
            target = _pick_target(path, base, out_dir, preserve_structure, used)

            # Carry existing markdown through (copy), don't re-convert.
            if _is_markdown(path):
                if out_dir is None or target.resolve() == path.resolve():
                    md_copied.append({"source": str(path), "markdown_file": str(path),
                                      "note": "already markdown (left in place)", "method": "copied-markdown"})
                    continue
                if target.exists() and not overwrite:
                    skipped.append({"source": str(path), "reason": f"exists: {target.name}"})
                    continue
                used.add(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                ch = len(target.read_text(encoding="utf-8", errors="replace"))
                md_copied.append({"source": str(path), "markdown_file": str(target),
                                  "bytes": target.stat().st_size, "chars": ch, "method": "copied-markdown"})
                total_chars += ch
                total_bytes += target.stat().st_size
                continue

            if target.exists() and not overwrite:
                skipped.append({"source": str(path),
                                "reason": f"exists: {target.name} (set overwrite=true to replace)"})
                continue
            text, meta = _convert_file(path, mode, lang, maxp)
            if not text.strip():
                # Distinguish genuine errors (couldn't process) from legit no-text output.
                err = meta.get("convert_error") or meta.get("ocr_error")
                if err:
                    failed.append({"source": str(path), "error": err})
                else:
                    empty.append({"source": str(path), "reason": _empty_reason(path, meta)})
                continue
            used.add(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            rec = {"source": str(path), "markdown_file": str(target),
                   "bytes": target.stat().st_size, "chars": len(text), "method": meta["method"]}
            if meta.get("ocr_used"):
                rec["ocr"] = True
                ocr_count += 1
            if meta.get("ocr_pages"):
                rec["ocr_pages"] = meta["ocr_pages"]
            if meta.get("pages"):
                rec["pages"] = meta["pages"]
            if meta.get("ocr_truncated"):
                rec["ocr_truncated"] = True
            if meta["method"] == "drive-link":
                pointers += 1
            converted.append(rec)
            total_chars += len(text)
            total_bytes += rec["bytes"]
        except Exception as e:  # noqa: BLE001
            failed.append({"source": str(path), "error": f"{type(e).__name__}: {e}"})

    totals = {
        "converted": len(converted), "markdown_copied": len(md_copied), "ocr_used": ocr_count,
        "drive_links": pointers, "empty": len(empty), "skipped": len(skipped),
        "failed": len(failed), "total_chars": total_chars, "total_md_bytes": total_bytes,
    }
    result = {
        "output_root": str(out_dir) if out_dir else "(beside each source file)",
        "summary": (f"{len(converted)} converted ({ocr_count} via OCR, {pointers} drive-links), "
                    f"{len(md_copied)} markdown copied, {len(empty)} empty, "
                    f"{len(skipped)} skipped, {len(failed)} failed"),
        "totals": totals,
        "ocr_available": _tesseract_ok(),
    }
    if write_index and (converted or md_copied) and out_dir is not None:
        result["index_file"] = str(_write_index(out_dir, converted + md_copied))

    # `empty`/`skipped`/`failed` are usually small and actionable — always include them.
    result["empty"] = empty
    result["skipped"] = skipped
    result["failed"] = failed
    if str(detail).lower() == "summary":
        result["sample"] = [{"source": Path(c["source"]).name, "markdown_file": c["markdown_file"]}
                            for c in (converted + md_copied)[:5]]
        result["note"] = ("detail='summary': per-file lists omitted to save tokens; "
                          "see index_file or call again with detail='full'.")
    else:
        result["converted"] = converted
        result["markdown_copied"] = md_copied
    return result


@mcp.tool()
def list_convertible_attachments(input_dir: Optional[str] = None, recursive: bool = True) -> dict:
    """List convertible files in a directory WITHOUT converting or reading them.

    Returns paths/extensions/sizes only (token-free). Google Drive pointer stubs and
    existing markdown files are reported separately (`pointers`, `markdown_count`).
    """
    d = _expand(input_dir) if input_dir else _env_dir("MARKITDOWN_INPUT_DIR")
    if not d:
        return {"error": "No input_dir provided and MARKITDOWN_INPUT_DIR is not set.", "files": []}
    if not d.is_dir():
        return {"error": f"Not a directory: {d}", "files": []}
    it = d.rglob("*") if recursive else d.glob("*")
    files, pointers, markdown, by_ext = [], [], [], {}
    for f in sorted(it):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in POINTER_EXTS:
            pointers.append({"path": str(f), "ext": ext})
        elif ext in MARKDOWN_EXTS:
            markdown.append({"path": str(f), "ext": ext})
        elif ext in CONVERTIBLE_EXTS:
            files.append({"path": str(f), "ext": ext, "bytes": f.stat().st_size})
            by_ext[ext] = by_ext.get(ext, 0) + 1
    return {"input_dir": str(d), "count": len(files), "by_ext": by_ext,
            "pointer_count": len(pointers), "markdown_count": len(markdown),
            "files": files, "pointers": pointers, "markdown": markdown}


@mcp.tool()
def convert_one(
    source: str,
    output_path: Optional[str] = None,
    overwrite: bool = True,
    ocr: Optional[str] = None,
    ocr_lang: Optional[str] = None,
    ocr_max_pages: Optional[int] = None,
) -> dict:
    """Convert a single file to a Markdown FILE on disk; returns only metadata (token-free).

    Supports OCR for images/scanned PDFs (`ocr`: auto/off/force/hybrid). If a different
    `.md` already exists at the default target, a type-qualified name (e.g. `name.pdf.md`)
    is used to avoid clobbering it.
    """
    src = _expand(source)
    if not src.is_file():
        return {"ok": False, "source": source, "error": "not found"}
    try:
        if output_path:
            target = _expand(output_path)
        else:
            target = src.with_suffix(".md")
            if target.exists() and not overwrite:
                target = src.with_name(src.stem + src.suffix + ".md")
        text, meta = _convert_file(src, _ocr_mode(ocr), _ocr_lang(ocr_lang), _ocr_max_pages(ocr_max_pages))
        if not text.strip():
            err = meta.get("convert_error") or meta.get("ocr_error")
            if err:
                return {"ok": False, "source": str(src), "error": err}
            return {"ok": True, "source": str(src), "written": False,
                    "reason": _empty_reason(src, meta), "chars": 0, "method": meta["method"]}
        if target.exists() and not overwrite:
            return {"ok": False, "source": str(src), "error": f"target exists: {target}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        out = {"ok": True, "source": str(src), "markdown_file": str(target),
               "bytes": target.stat().st_size, "chars": len(text), "method": meta["method"]}
        for k in ("ocr_used", "ocr_pages", "pages", "ocr_truncated"):
            if meta.get(k):
                out["ocr" if k == "ocr_used" else k] = meta[k]
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": str(src), "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def peek_markdown(markdown_file: str, max_chars: int = 400) -> dict:
    """OPT-IN: return a small, capped preview of a generated `.md` file.

    Capped at 4000 chars (default 400). For full content, read the file directly.
    """
    p = _expand(markdown_file)
    if not p.is_file():
        return {"ok": False, "error": "not found", "markdown_file": str(p)}
    max_chars = max(1, min(int(max_chars), 4000))
    data = p.read_text(encoding="utf-8", errors="replace")
    return {"ok": True, "markdown_file": str(p), "total_chars": len(data),
            "preview": data[:max_chars], "truncated": len(data) > max_chars}


@mcp.tool()
def ocr_capabilities() -> dict:
    """Report whether Tesseract OCR is available (path, version, installed languages)."""
    info = {"available": _tesseract_ok(), "tesseract_cmd": _tess_cmd()}
    if info["available"]:
        try:
            info["version"] = subprocess.run([_tess_cmd(), "--version"], capture_output=True
                                              ).stdout.decode("utf-8", "replace").splitlines()[0]
            langs = subprocess.run([_tess_cmd(), "--list-langs"], capture_output=True
                                   ).stdout.decode("utf-8", "replace").splitlines()[1:]
            info["languages"] = [l for l in langs if l.strip()]
        except Exception as e:  # noqa: BLE001
            info["note"] = f"{type(e).__name__}: {e}"
    else:
        info["hint"] = "Install Tesseract (macOS: brew install tesseract) to enable image/scanned-PDF OCR."
    return info


# --------------------------------------------------------------------------- #
# Self-test + entry point
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="mkid_selftest_"))
    (d / "a.csv").write_text("name,role\nAda,Engineer\nGrace,Admiral\n")
    (d / "b.html").write_text("<h1>ZZSENTINELZZ heading</h1><p>secret body text</p>")
    (d / "existing.md").write_text("# pre-existing note\n\nCarry me through.\n")
    (d / "ptr.gdoc").write_text('{"doc_id":"ABC123","email":"x@y.com"}')

    res = convert_attachments_to_markdown(input_dir=str(d), output_dir=str(d / "out"), write_index=True)
    print(json.dumps(res["totals"], indent=2))

    blob = json.dumps(res)
    conv_ok = res["totals"]["converted"] == 3            # csv, html, gdoc(link)
    md_ok = res["totals"]["markdown_copied"] == 1        # existing.md carried through
    no_leak = "ZZSENTINELZZ" not in blob and "secret body text" not in blob
    no_pii = "x@y.com" not in blob and "x@y.com" not in (d / "out" / "ptr.md").read_text()
    on_disk = any("ZZSENTINELZZ" in f.read_text() for f in (d / "out").glob("*.md"))
    link_ok = "docs.google.com/document/d/ABC123" in (d / "out" / "ptr.md").read_text()
    copied_ok = (d / "out" / "existing.md").exists() and "Carry me through" in (d / "out" / "existing.md").read_text()

    ok = conv_ok and md_ok and no_leak and no_pii and on_disk and link_ok and copied_ok
    print(f"SELFTEST: conv3={conv_ok} md_copied={md_ok} no_leak={no_leak} no_pii={no_pii} "
          f"on_disk={on_disk} link={link_ok} copied={copied_ok} -> {'PASS' if ok else 'FAIL'}")
    print("OCR:", _tesseract_ok(), "->", _tess_cmd())
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    mcp.run()


if __name__ == "__main__":
    main()
