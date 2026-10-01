# Document OCR MCP Server

A local MCP server for extracting text from PDF documents and images, including handwritten text, using Claude Vision API.

## Features

- **PDF Processing**: Convert PDF pages to images and extract text
- **Image OCR**: Direct image text extraction
- **Handwriting Support**: Optimized mode for handwritten text recognition
- **Multiple Output Modes**: Standard (structured), Compact (text only), Handwriting (optimized)
- **Image Enhancement**: Automatic preprocessing for better OCR results

## System Requirements

### Poppler (Required for PDF processing)

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install poppler-utils
```

**Linux (Arch):**
```bash
sudo pacman -S poppler
```

**macOS:**
```bash
brew install poppler
```

**Windows:**
1. Download from: https://github.com/oschwartz10612/poppler-windows/releases
2. Extract to `C:\poppler`
3. Add `C:\poppler\bin` to PATH

## Python Dependencies

```bash
pip install pdf2image Pillow anthropic
```

## Configuration

Set `ANTHROPIC_API_KEY` in the Agents-Core `.env` at the repository root
(create it with `cp env.example .env` if needed), or pass it in the MCP entry's
`env`. The server's `load_dotenv()` searches upward from `server.py`, so it reads
the root `.env`.

```env
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Tools

#### `extract_text_from_image`
Extract text from an image file.

```
Arguments:
- image_path: Path to image (JPG, PNG, TIFF, etc.)
- mode: "standard" (default) | "compact" | "handwriting"
- enhance: true (default) / false - preprocess image
```

#### `extract_text_from_pdf`
Extract text from PDF document.

```
Arguments:
- pdf_path: Path to PDF file
- pages: a range "1-5", a list "1,3,5", a single page "3", or null for all;
  mixed forms such as "1-3,5" return an error
- mode: "standard" (default) | "compact" | "handwriting"
- dpi: Resolution (default: 200)
```

Every PDF page is enhanced before OCR; this tool has no `enhance` argument.

#### `get_pdf_info`
Get PDF metadata (page count, size).

#### `check_dependencies`
Verify all dependencies are installed.

## Integration with Cursor

The installers do not register this server. Add it to your client configuration
by hand, for example Cursor's `.cursor/mcp.json` (project) or
`~/.cursor/mcp.json` (global), using absolute paths:

```json
{
  "mcpServers": {
    "document-ocr": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/mcp_servers/document_ocr/server.py"]
    }
  }
}
```

On Windows, point `command` at the absolute interpreter path, with forward
slashes or escaped backslashes in JSON, for example
`"C:/path/to/Agents/.venv/Scripts/python.exe"`.

## Examples

### Extract text from scanned document
```
Use tool: extract_text_from_image
image_path: "/path/to/scan.jpg"
mode: "standard"
```

### Process handwritten notes
```
Use tool: extract_text_from_image
image_path: "/path/to/notes.jpg"
mode: "handwriting"
```

### Batch process PDF
```
Use tool: extract_text_from_pdf
pdf_path: "/path/to/document.pdf"
pages: "1-10"
mode: "standard"
dpi: 300
```

## Troubleshooting

### "poppler not found"
- Ensure poppler-utils is installed
- Ensure `pdftoppm` is in PATH
- On Windows, add poppler/bin to PATH

### "API key not set"
- Check that the root `.env` exists or that the MCP entry's `env` sets the key
- Verify API key is correct
- Only Anthropic is supported: `server.py` fixes the provider and the model
  (`claude-sonnet-4-20250514`), so set `ANTHROPIC_API_KEY`;
  `check_dependencies` reports only whether it is non-empty, so the `sk-ant-...`
  placeholder from `env.example` also shows as set until you replace it

### Poor OCR quality
- Increase DPI for PDF (try 300)
- For images, keep `enhance` at its default `true`; PDF pages are always enhanced
- Try "handwriting" mode for handwritten text
- Check source image quality
