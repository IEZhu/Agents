"""
Document OCR MCP Server

A local MCP server for extracting text from PDF documents and images,
including handwritten text, using Vision API.

Dependencies:
- pdf2image (requires poppler-utils system package)
- Pillow
- anthropic (for Claude Vision)

System Requirements:
- Linux: sudo apt-get install poppler-utils
- macOS: brew install poppler
- Windows: Download from https://github.com/oschwartz10612/poppler-windows/releases
"""

import os
import io
import base64
import logging
import tempfile
from pathlib import Path
from typing import Optional, Literal

from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
SERVER_NAME = "document-ocr"
VISION_PROVIDER = "anthropic"
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB max for Vision API

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(SERVER_NAME)

# Initialize MCP Server
mcp = FastMCP(SERVER_NAME)

# ============================================================================
# Utility Functions
# ============================================================================

def image_to_base64(image) -> str:
    """Convert PIL Image to base64 string."""
    from PIL import Image

    buffer = io.BytesIO()
    # Convert to RGB if necessary (for PNG with alpha channel)
    if image.mode in ('RGBA', 'LA', 'P'):
        image = image.convert('RGB')
    image.save(buffer, format='JPEG', quality=95)
    return base64.b64encode(buffer.getvalue()).decode('utf-8')

def preprocess_image(image, enhance: bool = True):
    """
    Preprocess image for better OCR results.

    - Convert to grayscale for text
    - Enhance contrast
    - Resize if too large
    """
    from PIL import Image, ImageEnhance, ImageFilter

    # Resize if too large (Vision APIs have limits)
    max_dimension = 4096
    if max(image.size) > max_dimension:
        ratio = max_dimension / max(image.size)
        new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        logger.info(f"Resized image to {new_size}")

    if enhance:
        # Enhance sharpness for better text recognition
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(1.5)

        # Enhance contrast slightly
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.2)

    return image

async def call_vision_api(
    image_base64: str,
    prompt: str,
    provider: str = "anthropic"
) -> str:
    """
    Call Vision API (Claude Vision) to extract text from image.
    """
    if provider == "anthropic":
        return await _call_anthropic_vision(image_base64, prompt)
    else:
        raise ValueError(f"Unknown vision provider: {provider}")

async def _call_anthropic_vision(image_base64: str, prompt: str) -> str:
    """Call Claude Vision API."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    message = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    )

    return message.content[0].text

# ============================================================================
# OCR Prompts
# ============================================================================

OCR_PROMPT_STANDARD = """Extract ALL text from this image with maximum accuracy.

INSTRUCTIONS:
1. Identify the document type (form, letter, note, book page, etc.)
2. Preserve the original structure:
   - Headings → ## Heading
   - Paragraphs → Separate with blank lines
   - Lists → Use - or 1. 2. 3.
   - Tables → Use | col1 | col2 | format
3. For handwritten text:
   - Read carefully, considering context
   - If uncertain about a word, provide [best_guess?] notation
   - Preserve original spelling even if incorrect
4. For printed text:
   - Maintain exact formatting where possible
5. Note any:
   - Signatures → [Signature]
   - Stamps → [Stamp: description]
   - Illegible sections → [Illegible: ~N words]

OUTPUT: Return ONLY the extracted text in Markdown format. No explanations."""

OCR_PROMPT_COMPACT = """Extract all text from this image exactly as written.
Return ONLY the plain text, preserving line breaks.
For unclear words use [word?] notation.
No explanations or metadata."""

OCR_PROMPT_HANDWRITING = """This image contains handwritten text. Please extract it with maximum accuracy.

SPECIAL INSTRUCTIONS for handwriting:
1. Pay close attention to letter shapes - many handwriting styles are similar
2. Use context to disambiguate unclear words
3. Common issues to watch for:
   - 'a' vs 'o' vs 'u'
   - 'n' vs 'm' vs 'h'
   - 'l' vs '1' vs 'i'
   - 'r' vs 'v'
4. Mark genuinely illegible sections as [illegible]
5. For uncertain readings: [word?]

OUTPUT: Return the extracted text in Markdown format."""

# ============================================================================
# MCP Tools
# ============================================================================

@mcp.tool()
async def extract_text_from_image(
    image_path: str,
    mode: Literal["standard", "compact", "handwriting"] = "standard",
    enhance: bool = True
) -> str:
    """
    Extract text from an image file using Vision AI.

    Args:
        image_path: Path to the image file (JPG, PNG, TIFF, etc.)
        mode: OCR mode - "standard" (structured), "compact" (text only), "handwriting" (optimized for handwritten)
        enhance: Whether to preprocess image for better results

    Returns:
        Extracted text in Markdown format
    """
    from PIL import Image

    path = Path(image_path).expanduser().resolve()

    if not path.exists():
        return f"Error: File not found: {path}"

    if not path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp', '.heic']:
        return f"Error: Unsupported image format: {path.suffix}"

    try:
        # Load and preprocess image
        image = Image.open(path)
        logger.info(f"Loaded image: {path}, size: {image.size}, mode: {image.mode}")

        if enhance:
            image = preprocess_image(image, enhance=True)

        # Convert to base64
        image_b64 = image_to_base64(image)

        # Select prompt
        prompts = {
            "standard": OCR_PROMPT_STANDARD,
            "compact": OCR_PROMPT_COMPACT,
            "handwriting": OCR_PROMPT_HANDWRITING
        }
        prompt = prompts.get(mode, OCR_PROMPT_STANDARD)

        # Call Vision API
        result = await call_vision_api(image_b64, prompt, VISION_PROVIDER)

        return result

    except Exception as e:
        logger.error(f"OCR failed: {e}")
        return f"Error during OCR: {str(e)}"

@mcp.tool()
async def extract_text_from_pdf(
    pdf_path: str,
    pages: Optional[str] = None,
    mode: Literal["standard", "compact", "handwriting"] = "standard",
    dpi: int = 200
) -> str:
    """
    Extract text from a PDF document using Vision AI.

    Converts PDF pages to images and processes them with Vision API.
    Requires poppler-utils to be installed on the system.

    Args:
        pdf_path: Path to the PDF file
        pages: Page range to process (e.g., "1-5", "1,3,5", or None for all)
        mode: OCR mode - "standard", "compact", or "handwriting"
        dpi: Resolution for PDF to image conversion (higher = better quality, slower)

    Returns:
        Extracted text from all processed pages
    """
    from pdf2image import convert_from_path
    from PIL import Image

    path = Path(pdf_path).expanduser().resolve()

    if not path.exists():
        return f"Error: File not found: {path}"

    if path.suffix.lower() != '.pdf':
        return f"Error: Not a PDF file: {path}"

    try:
        # Parse page range
        first_page = None
        last_page = None
        specific_pages = None

        if pages:
            if '-' in pages:
                parts = pages.split('-')
                first_page = int(parts[0])
                last_page = int(parts[1])
            elif ',' in pages:
                specific_pages = [int(p.strip()) for p in pages.split(',')]
            else:
                first_page = int(pages)
                last_page = int(pages)

        # Convert PDF to images
        logger.info(f"Converting PDF to images: {path}, dpi={dpi}")

        if specific_pages:
            # Convert specific pages one by one
            images = []
            for page_num in specific_pages:
                page_images = convert_from_path(
                    path,
                    dpi=dpi,
                    first_page=page_num,
                    last_page=page_num
                )
                images.extend(page_images)
        else:
            images = convert_from_path(
                path,
                dpi=dpi,
                first_page=first_page,
                last_page=last_page
            )

        logger.info(f"Converted {len(images)} pages")

        # Process each page
        results = []
        prompts = {
            "standard": OCR_PROMPT_STANDARD,
            "compact": OCR_PROMPT_COMPACT,
            "handwriting": OCR_PROMPT_HANDWRITING
        }
        prompt = prompts.get(mode, OCR_PROMPT_STANDARD)

        for i, image in enumerate(images, 1):
            page_num = specific_pages[i-1] if specific_pages else (first_page or 1) + i - 1
            logger.info(f"Processing page {page_num}...")

            # Preprocess
            image = preprocess_image(image, enhance=True)

            # Convert to base64
            image_b64 = image_to_base64(image)

            # Call Vision API
            try:
                text = await call_vision_api(image_b64, prompt, VISION_PROVIDER)
                results.append(f"## Page {page_num}\n\n{text}")
            except Exception as e:
                results.append(f"## Page {page_num}\n\n[Error: {str(e)}]")

        return "\n\n---\n\n".join(results)

    except Exception as e:
        logger.error(f"PDF OCR failed: {e}")
        return f"Error during PDF OCR: {str(e)}"

@mcp.tool()
async def get_pdf_info(pdf_path: str) -> str:
    """
    Get information about a PDF file (page count, size, etc.)

    Args:
        pdf_path: Path to the PDF file

    Returns:
        PDF metadata as formatted string
    """
    from pdf2image.pdf2image import pdfinfo_from_path

    path = Path(pdf_path).expanduser().resolve()

    if not path.exists():
        return f"Error: File not found: {path}"

    try:
        info = pdfinfo_from_path(path)

        result = f"""## PDF Information

**File**: {path.name}
**Pages**: {info.get('Pages', 'Unknown')}
**Size**: {path.stat().st_size / 1024:.1f} KB
**Format**: PDF {info.get('PDF version', 'Unknown')}
"""
        return result

    except Exception as e:
        return f"Error reading PDF info: {str(e)}"

@mcp.tool()
async def check_dependencies() -> str:
    """
    Check if all required dependencies are installed.

    Returns:
        Status of each dependency
    """
    results = []

    # Check pdf2image
    try:
        import pdf2image
        results.append("✅ pdf2image: installed")
    except ImportError:
        results.append("❌ pdf2image: NOT installed (pip install pdf2image)")

    # Check Pillow
    try:
        from PIL import Image
        results.append("✅ Pillow: installed")
    except ImportError:
        results.append("❌ Pillow: NOT installed (pip install Pillow)")

    # Check poppler
    try:
        from pdf2image.pdf2image import pdfinfo_from_path
        # Try to get info from a non-existent file to check poppler
        import subprocess
        result = subprocess.run(['pdftoppm', '-v'], capture_output=True, text=True)
        if result.returncode == 0 or 'pdftoppm' in result.stderr:
            results.append("✅ poppler: installed")
        else:
            results.append("❌ poppler: NOT installed")
    except Exception:
        results.append("❌ poppler: NOT installed or not in PATH")

    # Check Vision API
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            results.append("✅ anthropic: installed, API key set")
        else:
            results.append("⚠️ anthropic: installed, but ANTHROPIC_API_KEY not set")
    except ImportError:
        results.append("❌ anthropic: NOT installed (pip install anthropic)")

    return "## Dependency Check\n\n" + "\n".join(results)

if __name__ == "__main__":
    mcp.run()
