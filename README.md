# MarkItDown Attachments — a token-free MCP server

Convert Claude **chat & project attachments** (and any local files) to Markdown
using Microsoft's [MarkItDown](https://github.com/microsoft/markitdown), **without
spending context tokens on the file contents**.

## Why this exists

The stock [`markitdown-mcp`](https://github.com/microsoft/markitdown/tree/main/packages/markitdown-mcp)
server returns the converted Markdown as the tool result — so it lands in the
conversation and consumes tokens. This server instead converts each file
**locally** and **writes a `.md` file to disk**, returning only compact metadata
(output paths, byte/character counts, status). Converting a folder full of
attachments therefore costs effectively **zero context tokens**; Claude can read
individual `.md` files later, only when their content is actually needed.

## Supported formats

PDF · Word (`.docx`) · PowerPoint (`.pptx`) · Excel (`.xlsx`/`.xls`) · images
(`.png/.jpg/...`, EXIF + OCR) · audio (`.wav/.mp3/...`, metadata + optional
transcription) · HTML · CSV/TSV · JSON · XML · EPub · ZIP (recurses) · Outlook
`.msg` · Jupyter `.ipynb`, and more.

## Tools

| Tool | What it does | Returns |
|------|--------------|---------|
| `convert_attachments_to_markdown` | Batch-convert files, directories, and globs to `.md` files | metadata only (paths/sizes/status) |
| `list_convertible_attachments` | Enumerate convertible files in a directory | paths/extensions/sizes |
| `convert_one` | Convert a single file to a `.md` file | metadata only |
| `peek_markdown` | **Opt-in** small preview of a generated `.md` (default 400 chars, capped at 4000) | short text snippet |

Only `peek_markdown` ever returns file text, and only a small capped slice you
explicitly request. The conversion tools never do.

## Configuration

Set via environment variables (Claude Code `.mcp.json` `env`) or the extension's
**user config** (Claude Desktop):

| Variable / config | Meaning |
|-------------------|---------|
| `MARKITDOWN_INPUT_DIR` / *Default attachments folder* | Folder scanned when you don't name specific files |
| `MARKITDOWN_OUTPUT_DIR` / *Markdown output folder* | Where `.md` files are written (default: next to each source) |
| `MARKITDOWN_ENABLE_PLUGINS` / *Enable MarkItDown plugins* | Allow third-party markitdown plugins (default off) |

## Install

### Build the runtime (once)

```bash
./install.sh        # creates .venv next to this README and installs deps
```

Requires Python **3.10–3.13** (3.12 recommended; `brew install python@3.12`).

### Claude Code

Add to your project's `.mcp.json` (paths are absolute; adjust to where you
cloned this):

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

Install the packaged extension `dist/markitdown-attachments.mcpb`:
**Settings → Extensions → Install from file…**, then pick the `.mcpb`. (The
bundle's manifest expects a `.venv` beside the server — run `install.sh` inside
the installed extension folder if needed.) Restart the app.

## Usage examples

> "Convert every attachment in `~/Downloads/contracts` to markdown."
> → `convert_attachments_to_markdown(input_dir="~/Downloads/contracts")`

> "Turn these two files into markdown next to them: report.pdf, deck.pptx"
> → `convert_attachments_to_markdown(sources=["report.pdf","deck.pptx"])`

Claude gets back a list of generated `.md` paths — no document text — and can
open any of them on demand.

## Credits

Built on [microsoft/markitdown](https://github.com/microsoft/markitdown) (MIT).
This wrapper is MIT-licensed.
