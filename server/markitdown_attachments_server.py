#!/usr/bin/env python3
"""
MarkItDown Attachments — a *token-free* MCP server.

Converts Claude chat / project attachments (and any local files) to Markdown
using Microsoft's `markitdown` library. Conversion runs entirely locally; the
resulting Markdown is WRITTEN TO DISK as `.md` files and only compact metadata
(file paths, byte/char counts, status) is returned to the model.

Why this matters: the stock `markitdown-mcp` server returns the converted
Markdown as the tool result, which lands in the conversation and consumes
context tokens. This server never returns file content from its conversion
tools, so pre-processing a folder full of attachments costs effectively zero
context tokens. The model (or user) can then read individual `.md` files only
when their content is actually needed.

Tools:
  - list_convertible_attachments : enumerate convertible files (paths/sizes only)
  - convert_attachments_to_markdown : batch convert files/dirs/globs -> .md files
  - convert_one : convert a single file -> .md file
  - peek_markdown : OPT-IN small, capped preview of a generated .md file

Configuration (environment variables; settable via DXT user_config or .mcp.json env):
  MARKITDOWN_INPUT_DIR       default directory scanned when no `sources` are given
  MARKITDOWN_OUTPUT_DIR      default directory for generated .md files
                             (if unset, each .md is written next to its source)
  MARKITDOWN_ENABLE_PLUGINS  "true"/"1"/"yes" to enable markitdown 3rd-party plugins
"""

import glob as _glob
import os
import sys
from pathlib import Path
from typing import Optional

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("markitdown-attachments")

# File types markitdown can meaningfully convert. Used when scanning directories.
CONVERTIBLE_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm",
    ".html", ".htm", ".csv", ".tsv", ".json", ".xml", ".rss", ".atom",
    ".epub", ".zip", ".msg", ".txt", ".rtf", ".ipynb",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".wav", ".mp3", ".m4a", ".flac",
}
# Never re-convert Markdown into Markdown.
SKIP_EXTS = {".md", ".markdown"}


def _plugins_enabled() -> bool:
    return os.getenv("MARKITDOWN_ENABLE_PLUGINS", "false").strip().lower() in ("true", "1", "yes")


def _md() -> MarkItDown:
    return MarkItDown(enable_plugins=_plugins_enabled())


def _expand(p: str) -> Path:
    """Expand ~ and $VARS, then resolve to an absolute path."""
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def _env_dir(var: str) -> Optional[Path]:
    v = os.getenv(var)
    return _expand(v) if v else None


def _glob_matches(pattern: str) -> list[Path]:
    expanded = os.path.expanduser(os.path.expandvars(pattern))
    return [Path(m) for m in _glob.glob(expanded, recursive=True)]


def _gather(sources: Optional[list[str]], input_dir: Optional[str], recursive: bool) -> list[Path]:
    """Resolve `sources` (files, dirs, globs) and/or a scan directory into an
    ordered, de-duplicated list of convertible file Paths."""
    files: list[Path] = []
    seen: set[Path] = set()

    def add_file(path: Path) -> None:
        rp = path.resolve()
        if rp in seen or rp.suffix.lower() in SKIP_EXTS:
            return
        seen.add(rp)
        files.append(rp)

    def add_dir(d: Path) -> None:
        it = d.rglob("*") if recursive else d.glob("*")
        for f in sorted(it):
            if f.is_file() and f.suffix.lower() in CONVERTIBLE_EXTS:
                add_file(f)

    if sources:
        for s in sources:
            matches = _glob_matches(s)
            for c in (matches if matches else [_expand(s)]):
                if c.is_dir():
                    add_dir(c)
                elif c.is_file():
                    add_file(c)
    else:
        d = _expand(input_dir) if input_dir else _env_dir("MARKITDOWN_INPUT_DIR")
        if d and d.is_dir():
            add_dir(d)
    return files


def _target_md_path(src: Path, out_dir: Optional[Path], used: set[Path]) -> Path:
    """Pick a .md target path, avoiding collisions within a single batch."""
    base = (src.with_suffix(".md") if out_dir is None else out_dir / (src.stem + ".md"))
    target = base
    n = 2
    while target in used:
        target = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    used.add(target)
    return target


@mcp.tool()
def convert_attachments_to_markdown(
    sources: Optional[list[str]] = None,
    input_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    recursive: bool = True,
    overwrite: bool = False,
) -> dict:
    """Convert attachments/files to Markdown FILES ON DISK without returning their content.

    Token-free: the converted Markdown is written to `.md` files and only compact
    metadata (file paths, byte/char counts, status) is returned — never the text
    itself. Use this to pre-process Claude chat/project attachments so their
    content can be read selectively later instead of being dumped into the chat.

    Args:
      sources: explicit file paths, directories, and/or glob patterns to convert.
               Directories are scanned for convertible files. If omitted, falls
               back to `input_dir` (or the MARKITDOWN_INPUT_DIR env var).
      input_dir: a directory to scan when `sources` is not provided.
      output_dir: where to write `.md` files. If omitted, uses MARKITDOWN_OUTPUT_DIR,
                  otherwise writes each `.md` next to its source file.
      recursive: recurse into subdirectories when scanning a directory (default True).
      overwrite: overwrite an existing `.md` target (default False -> such files are skipped).

    Returns: {output_root, converted:[{source, markdown_file, bytes, chars}],
              skipped:[{source, reason}], failed:[{source, error}], summary}
    """
    out_dir = _expand(output_dir) if output_dir else _env_dir("MARKITDOWN_OUTPUT_DIR")
    files = _gather(sources, input_dir, recursive)

    converted: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    used: set[Path] = set()
    md = _md()

    # Surface explicitly-named sources that don't exist.
    if sources:
        for s in sources:
            if not _expand(s).exists() and not _glob_matches(s):
                failed.append({"source": s, "error": "not found"})

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        try:
            target = _target_md_path(f, out_dir, used)
            if target.exists() and not overwrite:
                skipped.append({
                    "source": str(f),
                    "reason": f"target exists: {target.name} (set overwrite=true to replace)",
                })
                continue
            text = md.convert(str(f)).markdown
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            converted.append({
                "source": str(f),
                "markdown_file": str(target),
                "bytes": target.stat().st_size,
                "chars": len(text),
            })
        except Exception as e:  # noqa: BLE001 - report, don't crash the batch
            failed.append({"source": str(f), "error": f"{type(e).__name__}: {e}"})

    return {
        "output_root": str(out_dir) if out_dir else "(beside each source file)",
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "summary": f"{len(converted)} converted, {len(skipped)} skipped, {len(failed)} failed",
    }


@mcp.tool()
def list_convertible_attachments(input_dir: Optional[str] = None, recursive: bool = True) -> dict:
    """List convertible files in a directory WITHOUT converting or reading them.

    Returns paths, extensions, and sizes only (no content -> token-free). Handy to
    preview what `convert_attachments_to_markdown` would process.
    """
    d = _expand(input_dir) if input_dir else _env_dir("MARKITDOWN_INPUT_DIR")
    if not d:
        return {"error": "No input_dir provided and MARKITDOWN_INPUT_DIR is not set.", "files": []}
    if not d.is_dir():
        return {"error": f"Not a directory: {d}", "files": []}
    it = d.rglob("*") if recursive else d.glob("*")
    files = [
        {"path": str(f), "ext": f.suffix.lower(), "bytes": f.stat().st_size}
        for f in sorted(it)
        if f.is_file() and f.suffix.lower() in CONVERTIBLE_EXTS
    ]
    return {"input_dir": str(d), "count": len(files), "files": files}


@mcp.tool()
def convert_one(source: str, output_path: Optional[str] = None, overwrite: bool = True) -> dict:
    """Convert a single file to a Markdown FILE on disk; returns only metadata (token-free)."""
    src = _expand(source)
    if not src.is_file():
        return {"ok": False, "source": source, "error": "not found"}
    try:
        target = _expand(output_path) if output_path else src.with_suffix(".md")
        if target.exists() and not overwrite:
            return {"ok": False, "source": str(src), "error": f"target exists: {target}"}
        text = _md().convert(str(src)).markdown
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {
            "ok": True,
            "source": str(src),
            "markdown_file": str(target),
            "bytes": target.stat().st_size,
            "chars": len(text),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": str(src), "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def peek_markdown(markdown_file: str, max_chars: int = 400) -> dict:
    """OPT-IN: return a small, capped preview of a generated `.md` file.

    Capped at 4000 chars (default 400) so a glance stays cheap. For full content,
    read the file directly instead of pulling it through this server.
    """
    p = _expand(markdown_file)
    if not p.is_file():
        return {"ok": False, "error": "not found", "markdown_file": str(p)}
    max_chars = max(1, min(int(max_chars), 4000))
    data = p.read_text(encoding="utf-8", errors="replace")
    return {
        "ok": True,
        "markdown_file": str(p),
        "total_chars": len(data),
        "preview": data[:max_chars],
        "truncated": len(data) > max_chars,
    }


def _selftest() -> int:
    """Local sanity check: converts a small fixture set and verifies that
    (a) files are written and (b) NO source content leaks into the tool result."""
    import json
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="mkid_selftest_"))
    (d / "a.csv").write_text("name,role\nAda,Engineer\nGrace,Admiral\n")
    (d / "b.html").write_text("<h1>ZZSENTINELZZ heading</h1><p>secret body text</p>")
    (d / "notes.md").write_text("# already markdown\n")  # must be skipped

    res = convert_attachments_to_markdown(input_dir=str(d), output_dir=str(d / "out"))
    print(json.dumps(res, indent=2))

    blob = json.dumps(res)
    wrote_two = len(res["converted"]) == 2 and not res["failed"]
    no_leak = "ZZSENTINELZZ" not in blob and "secret body text" not in blob
    # And confirm the content really was written to disk:
    out_files = list((d / "out").glob("*.md"))
    on_disk = any("ZZSENTINELZZ" in f.read_text() for f in out_files)

    ok = wrote_two and no_leak and on_disk
    print(f"SELFTEST: wrote_two={wrote_two} no_leak={no_leak} on_disk={on_disk} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    mcp.run()  # STDIO transport


if __name__ == "__main__":
    main()
