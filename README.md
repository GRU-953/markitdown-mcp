# MarkItDown Attachments — a token-free MCP server (with OCR)

Convert Claude **chat & project attachments** (and any local files) to Markdown
using Microsoft's [MarkItDown](https://github.com/microsoft/markitdown), **without
spending context tokens on the file contents** — now with **OCR** for images and
scanned PDFs.

## Why this exists

The stock [`markitdown-mcp`](https://github.com/microsoft/markitdown/tree/main/packages/markitdown-mcp)
returns the converted Markdown as the tool result — so it lands in the
conversation and consumes tokens. This server instead converts each file
**locally** and **writes a `.md` file to disk**, returning only compact metadata
(paths, byte/char counts, status). Converting a folder of attachments therefore
costs effectively **zero context tokens**; Claude reads individual `.md` files
later, only when their content is actually needed.

> **Measured:** on a real 76-file / 94 MB corpus, one batch call returned a
> **25 KB** metadata result while writing **1.75 M characters** of Markdown to
> disk — **69× more content than tokens returned.**

## Supported formats

PDF · Word (`.docx`) · PowerPoint (`.pptx`) · Excel (`.xlsx`/`.xls`) · images
(`.png/.jpg/...` — **OCR'd**) · audio (`.wav/.mp3/...`) · HTML · CSV/TSV · JSON ·
XML · EPub · ZIP (recurses) · Outlook `.msg` · Jupyter `.ipynb`, and more.
Google Drive pointer files (`.gdoc/.gslides/.gsheet/.gdrive`) become clickable
"Open in Drive" link notes.

## Features

- **OCR** (Tesseract) for images and scanned/image-only PDFs, with automatic
  page-orientation detection (sideways scans are read correctly). The `ocr`
  argument: `auto` (default), `off`, `force`, or **`hybrid`** — per-page, keeping
  text pages and OCR-ing only the image pages of mixed PDFs (e.g. a 25 MB report
  went from 5.5 K → 8.6 K chars by recovering its chart/infographic pages).
- **Existing markdown carried through** — `.md`/`.markdown` files in the input are
  copied into the output set (structure-preserved) so a folder sweep yields a
  *complete* collection, not a partial one. (`include_existing_markdown`, default on.)
- **Drive pointer links** — `.gdoc` etc. become a small Markdown note with the
  Google Drive URL (no raw JSON, no embedded email).
- **Collision-safe names** — same-stem files of different types become
  `report.pdf.md` / `report.docx.md` instead of overwriting or opaque `-2`.
- **Structure-preserving output** — sub-folders are mirrored under `output_dir`.
- **Compact results for big sweeps** — `detail="summary"` returns just totals +
  failures + a small sample (~2 KB instead of ~30 KB); the full manifest is in INDEX.md.
- **Rich summary** — `converted / OCR'd / drive-links / markdown-copied / empty /
  skipped / failed` plus totals. Text-less-but-valid files (e.g. photos) are
  reported as `empty`; files that genuinely error (corrupt, truncated, 0-byte,
  unreadable, password-protected) are reported as `failed` — a batch never
  crashes on a bad file. Stress-tested against corrupt/malformed/unreadable inputs.

## Tools

| Tool | What it does | Returns |
|------|--------------|---------|
| `convert_attachments_to_markdown` | Batch-convert files/dirs/globs to `.md` (with OCR) | metadata only |
| `list_convertible_attachments` | Enumerate convertible files; pointers listed separately | paths/sizes |
| `convert_one` | Convert a single file to a `.md` | metadata only |
| `peek_markdown` | **Opt-in** small preview of a generated `.md` (≤4000 chars) | short snippet |
| `ocr_capabilities` | Report Tesseract availability / version / languages | small object |

Only `peek_markdown` ever returns file text, and only a small slice you request.

## Configuration

Set via environment variables (Claude Code `.mcp.json` `env`) or the extension's
**user config** (Claude Desktop):

| Variable / config | Meaning |
|-------------------|---------|
| `MARKITDOWN_INPUT_DIR` / *Default attachments folder* | Folder scanned when you don't name files |
| `MARKITDOWN_OUTPUT_DIR` / *Markdown output folder* | Where `.md` files go (default: next to source) |
| `MARKITDOWN_OCR` / *OCR mode* | `auto` (default) · `off` · `force` |
| `MARKITDOWN_OCR_LANG` / *OCR language(s)* | e.g. `eng` or `eng+ben` |
| `MARKITDOWN_OCR_MAX_PAGES` | Max PDF pages to OCR per file (default 50) |
| `MARKITDOWN_ENABLE_PLUGINS` / *Enable plugins* | Third-party markitdown plugins (default off) |

## Install

### Build the runtime (once)

```bash
./install.sh                       # creates .venv + installs Python deps
brew install tesseract             # OCR engine (optional but recommended)
# brew install tesseract-lang      # extra OCR languages (e.g. Bengali)
```

Requires Python **3.10–3.13** (3.12 recommended). Without Tesseract the server
still runs — it just reports OCR as unavailable.

### Claude Code

Add to your project's `.mcp.json` (absolute paths):

```json
{
  "mcpServers": {
    "markitdown-attachments": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["/abs/path/to/markitdown-attachments-mcp/server/markitdown_attachments_server.py"],
      "env": {}
    }
  }
}
```

Reload the project; approve the server when prompted.

### Claude Desktop

Install `dist/markitdown-attachments.mcpb` via **Settings → Extensions → Install
from file…**, then run `install.sh` inside the installed extension folder so its
`.venv` is created. Restart the app.

## Usage examples

> "Convert every attachment in `~/Documents/Lex-Adex` to markdown."
> → `convert_attachments_to_markdown(input_dir="~/Documents/Lex-Adex", write_index=True)`

> "OCR these scanned PDFs into markdown next to them."
> → `convert_attachments_to_markdown(sources=["scan1.pdf","scan2.pdf"], ocr="force")`

Claude gets back the list of generated `.md` paths — no document text — and opens
any of them on demand.

## Testing

`tests/test_harness.py` drives the server over the real MCP protocol against a
folder you point it at, checking every tool, OCR, pointer handling, collision
safety, idempotency, accuracy spot-checks, and token-free guarantees:

```bash
../.venv/bin/python tests/test_harness.py full /path/to/your/files
```

## Credits

Built on [microsoft/markitdown](https://github.com/microsoft/markitdown) (MIT) and
[Tesseract OCR](https://github.com/tesseract-ocr/tesseract). This wrapper is MIT-licensed.
