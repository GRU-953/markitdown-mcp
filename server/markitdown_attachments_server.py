#!/usr/bin/env python3
"""
MarkItDown Attachments — a *token-free* MCP server (OCR + local vision, parallel).

Converts Claude chat / project attachments (and any local files) to Markdown
using Microsoft's `markitdown` library. Conversion runs entirely locally; the
resulting Markdown is WRITTEN TO DISK as `.md` files and only compact metadata
(file paths, byte/char counts, status) is returned to the model — so converting
attachments costs effectively zero context tokens. This holds for every feature
below: OCR text and vision-model image descriptions are written to disk, never
returned into the conversation.

Features:
  • Parallel batch conversion (thread pool; `workers`). Output target paths are
    assigned single-threaded first, so collision-safe naming stays race-free.
  • OCR (Tesseract) for images and scanned/image-only PDFs, with auto orientation.
    `ocr`: "auto" (default), "off", "force", "hybrid" (per-page text+OCR for mixed
    PDFs). force/hybrid also OCR images embedded inside DOCX/PPTX/XLSX.
  • Local vision LLM (open-source, via Ollama) describes images that OCR can't
    read — runs locally, written to disk. `vision`: "auto" (default; describe
    text-less images when a local model is available), "off", "force". No cloud,
    no API keys, no tokens.
  • Existing .md/.markdown inputs are carried through; Google Drive pointer files
    become "Open in Drive" link notes; collision-safe names; structure-preserving
    output; `detail="summary"` for compact results on big sweeps.

Tools: convert_attachments_to_markdown, list_convertible_attachments,
       convert_one, peek_markdown, ocr_capabilities.

Config env (also settable via DXT user_config / .mcp.json env):
  MARKITDOWN_INPUT_DIR, MARKITDOWN_OUTPUT_DIR, MARKITDOWN_ENABLE_PLUGINS,
  MARKITDOWN_OCR (auto/off/force/hybrid), MARKITDOWN_OCR_LANG, MARKITDOWN_OCR_MAX_PAGES,
  MARKITDOWN_VISION (auto/off/force), MARKITDOWN_VISION_MODEL, OLLAMA_HOST,
  MARKITDOWN_WORKERS, TESSERACT_CMD.
"""

import base64
import glob as _glob
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac"}
POINTER_EXTS = {".gdoc", ".gslides", ".gsheet", ".gdrive"}
MARKDOWN_EXTS = {".md", ".markdown"}
OFFICE_ZIP_EXTS = {".docx", ".pptx", ".xlsx", ".xlsm"}
GATHER_EXTS = CONVERTIBLE_EXTS | POINTER_EXTS

OCR_DPI = 200
HYBRID_PAGE_MIN_CHARS = 80
# Kept simple — small vision models can return empty output for over-specified prompts.
VISION_PROMPT = "Describe this image in detail, and transcribe any visible text verbatim."

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _plugins_enabled() -> bool:
    return os.getenv("MARKITDOWN_ENABLE_PLUGINS", "false").strip().lower() in ("true", "1", "yes")


# Thread-local MarkItDown — each worker thread gets its own instance (markitdown
# is not guaranteed thread-safe; per-thread avoids shared-state races).
_MD_LOCAL = threading.local()
def _md() -> MarkItDown:
    md = getattr(_MD_LOCAL, "md", None)
    if md is None:
        md = MarkItDown(enable_plugins=_plugins_enabled())
        _MD_LOCAL.md = md
    return md


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


def _vision_mode(v: Optional[str]) -> str:
    v = (v or os.getenv("MARKITDOWN_VISION") or "auto").strip().lower()
    return v if v in ("auto", "off", "force") else "auto"


def _vision_model(v: Optional[str]) -> str:
    return (v or os.getenv("MARKITDOWN_VISION_MODEL") or "moondream").strip()


def _transcribe_mode(v: Optional[str]) -> str:
    v = (v or os.getenv("MARKITDOWN_TRANSCRIBE") or "auto").strip().lower()
    return v if v in ("auto", "off", "force") else "auto"


def _whisper_model(v: Optional[str]) -> str:
    return (v or os.getenv("MARKITDOWN_WHISPER_MODEL") or "base").strip()


def _pdf_tables_mode(v: Optional[str]) -> str:
    v = (v or os.getenv("MARKITDOWN_PDF_TABLES") or "auto").strip().lower()
    return v if v in ("auto", "off", "force") else "auto"


def _ollama_host() -> str:
    return os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def _workers(v: Optional[int]) -> int:
    if v is None:
        try:
            v = int(os.getenv("MARKITDOWN_WORKERS", "0"))
        except ValueError:
            v = 0
    return int(v) if v and v >= 1 else min(8, (os.cpu_count() or 4))


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
        _TESS_CMD = (os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
                     or next((p for p in ("/opt/homebrew/bin/tesseract",
                                           "/usr/local/bin/tesseract",
                                           "/usr/bin/tesseract") if os.path.exists(p)), "tesseract"))
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


def _table_to_md(rows) -> str:
    rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r]
            for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join("---" for _ in range(ncol)) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _pdf_with_tables(path: Path, max_pages: int):
    """Reconstruct a digital PDF with pdfplumber: non-table text + tables rendered
    as clean markdown tables (table regions removed from the text, so no
    duplication). Returns (text, n_tables)."""
    import pdfplumber
    parts, ntab = [], 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:max_pages]:
            tables = page.find_tables()
            bboxes = [t.bbox for t in tables]

            def _keep(o, _bb=bboxes):
                cx = (o.get("x0", 0) + o.get("x1", 0)) / 2
                cy = (o.get("top", 0) + o.get("bottom", 0)) / 2
                return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in _bb)

            try:
                txt = ((page.filter(_keep).extract_text() if bboxes else page.extract_text()) or "").strip()
            except Exception:
                txt = (page.extract_text() or "").strip()
            seg = [txt] if txt else []
            for t in tables:
                md = _table_to_md(t.extract())
                if md:
                    seg.append(md)
                    ntab += 1
            if seg:
                parts.append("\n\n".join(seg))
    return "\n\n".join(parts), ntab


_OFFICE_MEDIA_RE = re.compile(r"(?:word|ppt|xl)/media/", re.I)
_OFFICE_IMG_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")


def _office_ocr(path: Path, lang: str, max_images: int, min_bytes: int = 5000):
    """OCR raster images embedded inside a zip-based Office file (docx/pptx/xlsx)."""
    import zipfile
    from PIL import Image
    with zipfile.ZipFile(str(path)) as z:
        names = sorted(n for n in z.namelist()
                       if _OFFICE_MEDIA_RE.search(n)
                       and n.lower().endswith(_OFFICE_IMG_SUFFIX)
                       and z.getinfo(n).file_size >= min_bytes)
        parts, nwith = [], 0
        for n in names[:max_images]:
            try:
                t = _ocr_pil(Image.open(io.BytesIO(z.read(n))), lang).strip()
            except Exception:  # noqa: BLE001
                t = ""
            if sum(c.isalnum() for c in t) >= 12:
                nwith += 1
                parts.append(f"### {n.split('/')[-1]}\n\n{t}")
        return "\n\n".join(parts), len(names), nwith, len(names) > max_images


# --------------------------------------------------------------------------- #
# Local vision model (Ollama) — open-source, local, token-free
# --------------------------------------------------------------------------- #

# --- Ollama lifecycle: start on demand, stop after idle (resource-friendly) ----
_OLLAMA_IDLE_SECS = int(os.getenv("OLLAMA_IDLE_TIMEOUT", "300") or "300")
_STATE_DIR = Path(os.path.expanduser("~/.markitdown-attachments"))
_ACTIVITY_FILE = _STATE_DIR / "ollama_last_use"
_OLLAMA_PROC = None            # the `ollama serve` WE started (main process only)
_WATCHDOG_ON = False
_OLLA_LOCK = threading.Lock()


def _ollama_up(timeout: float = 2.0) -> bool:
    try:
        import requests
        return requests.get(_ollama_host() + "/api/version", timeout=timeout).status_code == 200
    except Exception:
        return False


def _ollama_available() -> bool:
    return _ollama_up()


def _touch_ollama_activity() -> None:
    """Record 'just used' — written by the main process and by pool workers, so the
    idle watchdog (main process) tracks activity across process boundaries."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _ACTIVITY_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _ollama_idle_secs() -> float:
    try:
        return time.time() - _ACTIVITY_FILE.stat().st_mtime
    except Exception:
        return float("inf")


def _ollama_bin() -> str:
    return (shutil.which("ollama")
            or next((p for p in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama")
                     if os.path.exists(p)), "ollama"))


def _stop_ollama() -> None:
    """Stop the `ollama serve` WE started — never one the user/brew is running."""
    global _OLLAMA_PROC
    with _OLLA_LOCK:
        proc, _OLLAMA_PROC = _OLLAMA_PROC, None
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _start_watchdog() -> None:
    global _WATCHDOG_ON
    with _OLLA_LOCK:
        if _WATCHDOG_ON:
            return
        _WATCHDOG_ON = True

    def loop():
        interval = min(30, max(5, _OLLAMA_IDLE_SECS // 4))
        while True:
            time.sleep(interval)
            try:
                if _OLLAMA_PROC is not None and _ollama_idle_secs() > _OLLAMA_IDLE_SECS:
                    _stop_ollama()
            except Exception:
                pass

    threading.Thread(target=loop, name="ollama-idle-watchdog", daemon=True).start()


def _ensure_ollama(timeout: float = 25.0) -> bool:
    """Start Ollama on demand (call from the MAIN process only) and arm the idle
    watchdog. Reuses an already-running instance; never stops one we didn't start.
    Returns True if the API is reachable."""
    _touch_ollama_activity()
    if _ollama_up():
        _start_watchdog()
        return True
    global _OLLAMA_PROC
    try:
        env = dict(os.environ)
        env.setdefault("OLLAMA_KEEP_ALIVE", "5m")   # model unloads even if the daemon lingers
        proc = subprocess.Popen([_ollama_bin(), "serve"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        with _OLLA_LOCK:
            _OLLAMA_PROC = proc
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ollama_up(timeout=1.0):
            _touch_ollama_activity()
            _start_watchdog()
            return True
        time.sleep(0.5)
    return _ollama_up()


def _vision_caption(path: Path, model: str, host: str, timeout: int = 180) -> str:
    """Describe an image with a local Ollama vision model. Returns the description."""
    import requests
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((1536, 1536))                 # downscale large photos for speed
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    # Try the detailed prompt, then a minimal fallback (some images yield empty on the former).
    for prompt in (VISION_PROMPT, "Describe this image."):
        r = requests.post(f"{host}/api/generate",
                          json={"model": model, "prompt": prompt, "images": [b64],
                                "stream": False, "options": {"temperature": 0.0}},
                          timeout=timeout)
        r.raise_for_status()
        resp = (r.json().get("response") or "").strip()
        if resp:
            return resp
    return ""


def _ollama_models() -> list:
    try:
        import requests
        data = requests.get(_ollama_host() + "/api/tags", timeout=3).json()
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


# --- Local speech-to-text (faster-whisper) — open-source, local, offline -------
_WHISPER = None
_WHISPER_LOCK = threading.Lock()


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _whisper(model_name: str):
    """Lazy-load and cache a local Whisper model (CPU int8 — fast, no GPU/torch)."""
    global _WHISPER
    if _WHISPER is None or _WHISPER[0] != model_name:
        with _WHISPER_LOCK:
            if _WHISPER is None or _WHISPER[0] != model_name:
                from faster_whisper import WhisperModel
                _WHISPER = (model_name, WhisperModel(model_name, device="cpu", compute_type="int8"))
    return _WHISPER[1]


def _transcribe_audio(path: Path, model_name: str):
    model = _whisper(model_name)
    segments, info = model.transcribe(str(path), beam_size=1)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, getattr(info, "duration", None), getattr(info, "language", None)


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
    if ext in IMAGE_EXTS:
        return base_len < 1
    if ext == ".pdf":
        return base_len < 50
    return False


def _convert_file(path: Path, ocr: str, lang: str, max_pages: int,
                  vision: str = "off", vmodel: str = "moondream",
                  transcribe: str = "auto", whisper_model: str = "base",
                  pdf_tables: str = "auto"):
    """Convert one file to markdown text. Returns (text, meta)."""
    ext = path.suffix.lower()
    meta = {"method": "markitdown", "ocr_used": False, "is_image": ext in IMAGE_EXTS,
            "pages": None, "ocr_truncated": False}

    if _is_pointer(path):
        meta["method"] = "drive-link"
        return _pointer_markdown(path), meta

    # Audio: transcribe LOCALLY with Whisper — never markitdown's cloud Google path.
    if ext in AUDIO_EXTS:
        if transcribe != "off" and _whisper_available():
            try:
                tr, dur, langd = _transcribe_audio(path, whisper_model)
                hdr = f"_(Audio transcript — local Whisper `{whisper_model}`"
                if dur:
                    hdr += f", {dur:.0f}s"
                if langd:
                    hdr += f", {langd}"
                hdr += ")_"
                meta["method"] = "whisper"
                meta["transcribed"] = True
                return f"{hdr}\n\n{tr if tr else '_(No speech detected.)_'}\n", meta
            except Exception as e:  # noqa: BLE001
                meta["convert_error"] = f"whisper: {type(e).__name__}: {e}"
                return "", meta
        meta["method"] = "audio (no transcription)"
        note = ("local transcription disabled" if transcribe == "off"
                else "install faster-whisper for local transcription")
        return f"_(Audio file — {note}.)_\n", meta

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
        except Exception as e:  # noqa: BLE001
            meta["ocr_error"] = f"{type(e).__name__}: {e}"

    # Digital PDF: reconstruct tables with pdfplumber (clean markdown tables, no
    # duplication). Scanned/near-empty PDFs fall through to markitdown + OCR.
    if ext == ".pdf" and pdf_tables in ("auto", "force"):
        try:
            pt, ntab = _pdf_with_tables(path, max_pages)
            if pt.strip() and (ntab > 0 or len(pt) >= 50):
                meta["method"] = "pdf+tables" if ntab else "pdf-text"
                if ntab:
                    meta["pdf_tables"] = ntab
                return pt, meta
        except Exception as e:  # noqa: BLE001 — fall through to markitdown
            meta.setdefault("pdf_tables_error", f"{type(e).__name__}: {e}")

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

    # Embedded-image OCR for Office documents (opt-in via ocr="force"/"hybrid").
    if ocr in ("force", "hybrid") and ext in OFFICE_ZIP_EXTS and _tesseract_ok():
        try:
            emb, _total, n_with, trunc = _office_ocr(path, lang, max_pages)
            if emb.strip():
                text = (text + "\n\n" if text.strip() else "") + "## Embedded images (OCR)\n\n" + emb
                meta["ocr_used"] = True
                meta["embedded_images_ocr"] = n_with
                meta["method"] = meta["method"] + "+img-ocr"
                if trunc:
                    meta["ocr_truncated"] = True
        except Exception as e:  # noqa: BLE001
            meta.setdefault("ocr_error", f"embedded-image OCR: {type(e).__name__}: {e}")

    # Local vision-model description for images OCR can't read (token-free; on disk).
    if ext in IMAGE_EXTS and vision != "off":
        want = (vision == "force") or (vision == "auto" and not text.strip())
        if want and _ollama_available():
            _touch_ollama_activity()
            try:
                cap = _vision_caption(path, vmodel, _ollama_host())
                if cap:
                    block = f"_(Image description — local vision model `{vmodel}`)_\n\n{cap}"
                    text = (text + "\n\n" if text.strip() else "") + block
                    meta["vision_used"] = True
                    meta["method"] = (meta["method"] + "+vision") if text != block else "vision"
            except Exception as e:  # noqa: BLE001
                meta["vision_error"] = f"{type(e).__name__}: {e}"

    return text, meta


def _empty_reason(path: Path, meta: dict) -> str:
    if path.suffix.lower() in IMAGE_EXTS and not _tesseract_ok():
        return "image with no embedded text — install tesseract to OCR it"
    if path.suffix.lower() in IMAGE_EXTS:
        return "image — OCR found no readable text (enable vision for a description)"
    return "no extractable text (may be scanned; retry with ocr='force' or 'hybrid')"


# --------------------------------------------------------------------------- #
# Gathering inputs + choosing output paths
# --------------------------------------------------------------------------- #

def _gather(sources, input_dir, recursive, include_markdown=False):
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
        if c.get("vision"):
            tags.append("vision")
        if c.get("method") == "copied-markdown":
            tags.append("copied")
        tag = (" · " + ", ".join(tags)) if tags else ""
        lines.append(f"- [{Path(c['source']).name}]({rel}) — {c.get('chars', 0)} chars{tag}")
    idx = out_dir / "INDEX.md"
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return idx


def _do_item(path: Path, target: Path, is_md: bool, mode: str, lang: str, maxp: int,
             vision: str, vmodel: str, transcribe: str = "auto", wmodel: str = "base",
             pdf_tables: str = "auto"):
    """Worker: convert/copy ONE pre-targeted item. Returns (category, record).
    Writes content to disk; returns only metadata (token-free, thread-safe)."""
    try:
        if is_md:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            ch = len(target.read_text(encoding="utf-8", errors="replace"))
            return ("copied", {"source": str(path), "markdown_file": str(target),
                               "bytes": target.stat().st_size, "chars": ch, "method": "copied-markdown"})
        text, meta = _convert_file(path, mode, lang, maxp, vision, vmodel, transcribe, wmodel, pdf_tables)
        if not text.strip():
            err = meta.get("convert_error") or meta.get("ocr_error") or meta.get("vision_error")
            if err:
                return ("failed", {"source": str(path), "error": err})
            return ("empty", {"source": str(path), "reason": _empty_reason(path, meta)})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        rec = {"source": str(path), "markdown_file": str(target),
               "bytes": target.stat().st_size, "chars": len(text), "method": meta["method"]}
        for mk, ok_key in (("ocr_used", "ocr"), ("ocr_pages", "ocr_pages"), ("pages", "pages"),
                           ("ocr_truncated", "ocr_truncated"), ("embedded_images_ocr", "embedded_images_ocr"),
                           ("vision_used", "vision"), ("pdf_tables", "pdf_tables")):
            if meta.get(mk):
                rec[ok_key] = meta[mk]
        return ("converted", rec)
    except Exception as e:  # noqa: BLE001
        return ("failed", {"source": str(path), "error": f"{type(e).__name__}: {e}"})


def _do_item_tuple(args):
    """Picklable top-level wrapper so _do_item can run in a ProcessPoolExecutor worker."""
    return _do_item(*args)


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
    vision: Optional[str] = None,
    vision_model: Optional[str] = None,
    transcribe: Optional[str] = None,
    whisper_model: Optional[str] = None,
    pdf_tables: Optional[str] = None,
    workers: Optional[int] = None,
    preserve_structure: bool = True,
    write_index: bool = False,
    include_existing_markdown: bool = True,
    detail: str = "full",
) -> dict:
    """Convert attachments/files to Markdown FILES ON DISK without returning their content.

    Token-free: converted Markdown (incl. OCR text and local vision-model image
    descriptions) is written to `.md` files; only compact metadata is returned.
    Files are converted in parallel across a thread pool.

    Args:
      sources: explicit file paths, directories, and/or glob patterns. If omitted,
               falls back to `input_dir` (or MARKITDOWN_INPUT_DIR).
      input_dir: directory to scan when `sources` is not provided.
      output_dir: where to write `.md` files (else MARKITDOWN_OUTPUT_DIR, else beside source).
      recursive: recurse into sub-directories when scanning (default True).
      overwrite: overwrite an existing `.md` target (default False -> skipped).
      ocr: "auto" (default), "off", "force", or "hybrid" (per-page text+OCR for mixed PDFs).
           force/hybrid also OCR images embedded inside Office files.
      ocr_lang: Tesseract language code(s), e.g. "eng" or "eng+ben".
      ocr_max_pages: cap on PDF pages OCR'd per file (default 50).
      vision: local-LLM image description: "auto" (default; describe text-less images
              when a local Ollama vision model is available), "off", or "force" (describe
              every image). Runs locally — no cloud, no tokens. No-ops if Ollama is absent.
      vision_model: Ollama vision model name (default "moondream").
      workers: parallel worker threads (default = min(8, CPU cores); 1 = sequential).
      preserve_structure: mirror source sub-folders under output_dir (default True).
      write_index: also write an INDEX.md linking every output file (output_dir only).
      include_existing_markdown: copy existing .md/.markdown inputs into the output (default True).
      detail: "full" (default) or "summary" (omit per-file arrays to save tokens).

    Returns: {output_root, summary, totals, ocr_available, vision_available,
              converted[]?, markdown_copied[]?, empty[], skipped[], failed[], index_file?}
    """
    out_dir = _expand(output_dir) if output_dir else _env_dir("MARKITDOWN_OUTPUT_DIR")
    mode, lang, maxp = _ocr_mode(ocr), _ocr_lang(ocr_lang), _ocr_max_pages(ocr_max_pages)
    vmode, vmodel = _vision_mode(vision), _vision_model(vision_model)
    tmode, wmodel = _transcribe_mode(transcribe), _whisper_model(whisper_model)
    ptmode = _pdf_tables_mode(pdf_tables)
    nworkers = _workers(workers)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    items = _gather(sources, input_dir, recursive, include_markdown=include_existing_markdown)

    # Start the local vision model on demand — only if vision is on AND images are present.
    if vmode != "off" and any(p.suffix.lower() in IMAGE_EXTS for p, _ in items):
        _ensure_ollama()
    # Pre-load Whisper once (avoids a model-download race across pool workers).
    if tmode != "off" and _whisper_available() and any(p.suffix.lower() in AUDIO_EXTS for p, _ in items):
        try:
            _whisper(wmodel)
        except Exception:
            pass

    converted, md_copied, empty, skipped, failed = [], [], [], [], []
    if sources:
        for s in sources:
            if not _expand(s).exists() and not _glob_matches(s):
                failed.append({"source": s, "error": "not found"})

    # Pass 1 (single-threaded): assign collision-safe targets, resolve skips / in-place md.
    used: set = set()
    work = []   # (path, target, is_md)
    for path, base in items:
        target = _pick_target(path, base, out_dir, preserve_structure, used)
        if _is_markdown(path):
            if out_dir is None or target.resolve() == path.resolve():
                md_copied.append({"source": str(path), "markdown_file": str(path),
                                  "note": "already markdown (left in place)", "method": "copied-markdown"})
                continue
            if target.exists() and not overwrite:
                skipped.append({"source": str(path), "reason": f"exists: {target.name}"})
                continue
            used.add(target)
            work.append((path, target, True))
            continue
        if target.exists() and not overwrite:
            skipped.append({"source": str(path),
                            "reason": f"exists: {target.name} (set overwrite=true to replace)"})
            continue
        used.add(target)
        work.append((path, target, False))

    # Pass 2 (parallel): convert/copy. Each worker writes to disk, returns metadata.
    # Use a PROCESS pool — markitdown's PDF/Office parsing is CPU-bound and GIL-limited,
    # so threads barely help; processes give true multi-core speedup. Workers re-import
    # this module as "__mp_main__" (spawn), so main() does NOT re-launch the server.
    # Falls back to a thread pool, then sequential, if a process pool can't start.
    results = None
    if nworkers > 1 and len(work) >= 4:
        args = [(p, t, is_md, mode, lang, maxp, vmode, vmodel, tmode, wmodel, ptmode) for (p, t, is_md) in work]
        for Pool in (ProcessPoolExecutor, ThreadPoolExecutor):
            try:
                with Pool(max_workers=nworkers) as ex:
                    results = list(ex.map(_do_item_tuple, args))
                break
            except Exception:  # noqa: BLE001
                results = None
    if results is None:
        results = [_do_item(p, t, is_md, mode, lang, maxp, vmode, vmodel, tmode, wmodel, ptmode) for (p, t, is_md) in work]

    ocr_count = pointers = vision_count = total_chars = total_bytes = 0
    for cat, rec in results:
        if cat == "converted":
            converted.append(rec)
            total_chars += rec.get("chars", 0)
            total_bytes += rec.get("bytes", 0)
            if rec.get("ocr"):
                ocr_count += 1
            if rec.get("vision"):
                vision_count += 1
            if rec.get("method") == "drive-link":
                pointers += 1
        elif cat == "copied":
            md_copied.append(rec)
            total_chars += rec.get("chars", 0)
            total_bytes += rec.get("bytes", 0)
        elif cat == "empty":
            empty.append(rec)
        else:
            failed.append(rec)

    totals = {
        "converted": len(converted), "markdown_copied": len(md_copied), "ocr_used": ocr_count,
        "vision_used": vision_count, "drive_links": pointers, "empty": len(empty),
        "skipped": len(skipped), "failed": len(failed),
        "total_chars": total_chars, "total_md_bytes": total_bytes,
    }
    result = {
        "output_root": str(out_dir) if out_dir else "(beside each source file)",
        "summary": (f"{len(converted)} converted ({ocr_count} OCR, {vision_count} vision, "
                    f"{pointers} drive-links), {len(md_copied)} markdown copied, "
                    f"{len(empty)} empty, {len(skipped)} skipped, {len(failed)} failed "
                    f"[{nworkers} workers]"),
        "totals": totals,
        "ocr_available": _tesseract_ok(),
        "vision_available": _ollama_available(),
    }
    if write_index and (converted or md_copied) and out_dir is not None:
        result["index_file"] = str(_write_index(out_dir, converted + md_copied))
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
    vision: Optional[str] = None,
    vision_model: Optional[str] = None,
    transcribe: Optional[str] = None,
    whisper_model: Optional[str] = None,
    pdf_tables: Optional[str] = None,
) -> dict:
    """Convert a single file to a Markdown FILE on disk; returns only metadata (token-free).

    Supports OCR (`ocr`: auto/off/force/hybrid) and local-LLM image description
    (`vision`: auto/off/force). A type-qualified name (e.g. `name.pdf.md`) avoids
    clobbering a different existing `.md`.
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
        vmode = _vision_mode(vision)
        if vmode != "off" and src.suffix.lower() in IMAGE_EXTS:
            _ensure_ollama()
        text, meta = _convert_file(src, _ocr_mode(ocr), _ocr_lang(ocr_lang), _ocr_max_pages(ocr_max_pages),
                                   vmode, _vision_model(vision_model),
                                   _transcribe_mode(transcribe), _whisper_model(whisper_model),
                                   _pdf_tables_mode(pdf_tables))
        if not text.strip():
            err = meta.get("convert_error") or meta.get("ocr_error") or meta.get("vision_error")
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
        for k in ("ocr_used", "ocr_pages", "pages", "ocr_truncated", "embedded_images_ocr", "vision_used", "pdf_tables"):
            if meta.get(k):
                out["ocr" if k == "ocr_used" else ("vision" if k == "vision_used" else k)] = meta[k]
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": str(src), "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def peek_markdown(markdown_file: str, max_chars: int = 400) -> dict:
    """OPT-IN: return a small, capped preview of a generated `.md` file (default 400, max 4000)."""
    p = _expand(markdown_file)
    if not p.is_file():
        return {"ok": False, "error": "not found", "markdown_file": str(p)}
    max_chars = max(1, min(int(max_chars), 4000))
    data = p.read_text(encoding="utf-8", errors="replace")
    return {"ok": True, "markdown_file": str(p), "total_chars": len(data),
            "preview": data[:max_chars], "truncated": len(data) > max_chars}


@mcp.tool()
def ocr_capabilities() -> dict:
    """Report local OCR (Tesseract) and vision (Ollama) availability + config."""
    info = {"ocr": {"available": _tesseract_ok(), "tesseract_cmd": _tess_cmd()}}
    if info["ocr"]["available"]:
        try:
            info["ocr"]["version"] = subprocess.run([_tess_cmd(), "--version"], capture_output=True
                                                    ).stdout.decode("utf-8", "replace").splitlines()[0]
            langs = subprocess.run([_tess_cmd(), "--list-langs"], capture_output=True
                                   ).stdout.decode("utf-8", "replace").splitlines()[1:]
            info["ocr"]["languages"] = [l for l in langs if l.strip()]
        except Exception as e:  # noqa: BLE001
            info["ocr"]["note"] = f"{type(e).__name__}: {e}"
    else:
        info["ocr"]["hint"] = "macOS: brew install tesseract (+ tesseract-lang for more languages)."
    vis = {"available": _ollama_available(), "host": _ollama_host(), "default_model": _vision_model(None),
           "lifecycle": "on-demand start + idle auto-stop",
           "idle_timeout_secs": _OLLAMA_IDLE_SECS,
           "managed_by_server": _OLLAMA_PROC is not None,
           "idle_secs": round(_ollama_idle_secs(), 1) if _ACTIVITY_FILE.exists() else None}
    if vis["available"]:
        vis["models_installed"] = _ollama_models()
    else:
        vis["hint"] = "Install Ollama (brew install ollama; brew services start ollama) and pull a vision model (ollama pull moondream)."
    info["vision"] = vis
    info["transcription"] = {"available": _whisper_available(), "engine": "faster-whisper (local)",
                             "default_model": _whisper_model(None)}
    info["workers_default"] = _workers(None)
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

    res = convert_attachments_to_markdown(input_dir=str(d), output_dir=str(d / "out"),
                                          write_index=True, workers=4, vision="off")
    print(json.dumps(res["totals"], indent=2))
    blob = json.dumps(res)
    conv_ok = res["totals"]["converted"] == 3
    md_ok = res["totals"]["markdown_copied"] == 1
    no_leak = "ZZSENTINELZZ" not in blob and "secret body text" not in blob
    no_pii = "x@y.com" not in blob and "x@y.com" not in (d / "out" / "ptr.md").read_text()
    on_disk = any("ZZSENTINELZZ" in f.read_text() for f in (d / "out").glob("*.md"))
    link_ok = "docs.google.com/document/d/ABC123" in (d / "out" / "ptr.md").read_text()
    copied_ok = (d / "out" / "existing.md").exists() and "Carry me through" in (d / "out" / "existing.md").read_text()

    ok = conv_ok and md_ok and no_leak and no_pii and on_disk and link_ok and copied_ok
    print(f"SELFTEST: conv3={conv_ok} md_copied={md_ok} no_leak={no_leak} no_pii={no_pii} "
          f"on_disk={on_disk} link={link_ok} copied={copied_ok} -> {'PASS' if ok else 'FAIL'}")
    print("OCR:", _tesseract_ok(), "| Vision(Ollama):", _ollama_available(),
          _ollama_models() if _ollama_available() else "")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    mcp.run()


if __name__ == "__main__":
    main()
