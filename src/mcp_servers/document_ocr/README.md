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

Copy `.env.example` to `.env` and configure:

```env
# API Keys
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Tools

#### `extract_text_from_image`
Extract text from an image file.

```
Arguments:
- image_path: Path to image (JPG, PNG, TIFF, etc.)
- mode: "standard" | "compact" | "handwriting"
- enhance: true/false - preprocess image
```

#### `extract_text_from_pdf`
Extract text from PDF document.

```
Arguments:
- pdf_path: Path to PDF file
- pages: "1-5", "1,3,5", or null for all
- mode: "standard" | "compact" | "handwriting"
- dpi: Resolution (default: 200)
```

#### `get_pdf_info`
Get PDF metadata (page count, size).

#### `check_dependencies`
Verify all dependencies are installed.

## Integration with Cursor

Add to `mcp.json`:

```json
{
  "mcpServers": {
    "document-ocr": {
      "command": ".venv/bin/python",
      "args": ["src/mcp_servers/document_ocr/server.py"]
    }
  }
}
```

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
- Check `.env` file exists
- Verify API key is correct
- Check `OCR_VISION_PROVIDER` matches your API key

### Poor OCR quality
- Increase DPI for PDF (try 300)
- Ensure `enhance: true` is set
- Try "handwriting" mode for handwritten text
- Check source image quality
