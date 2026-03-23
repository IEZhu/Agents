# /ocr

Activation of Document OCR Expert mode.
See rules: `.cursor/rules/10-document-ocr-expert.mdc`
See rules: `.cursor/rules/00-router.mdc`

Profile: **Document OCR Expert**.

## Usage

### Extract text from an image:
1. Open or attach an image in Cursor
2. Type `/ocr` and describe what you need
3. Example: `/ocr extract text from this image`

### Extract text from PDF:
1. Provide path to PDF or attach it
2. Type `/ocr`
3. Example: `/ocr extract text from document.pdf`

### Options:
- `compact` - text only, without metadata
- `structured` - preserving structure (default)
- `json` - JSON format output

## Examples

```
/ocr recognize handwritten notes
/ocr extract text from scan, compact
/ocr what is written on this picture?
```
