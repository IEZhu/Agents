# /init_repo

Initialize the Agents repository after cloning.
See rules: `.cursor/rules/10-ai-senior-engineer.mdc`
See rules: `.cursor/rules/00-router.mdc`
See rules: `.cursor/rules/99-environment.mdc`

Profile: **AI Senior Engineer (HAI)**

## Quick Start

Run the initialization script:

```bash
./scripts/init_repo.sh
```

## What It Does

1. **Pre-flight Checks**
   - Validates Python version (>= 3.10)
   - Checks pip availability

2. **Virtual Environment**
   - Creates `.venv/` at repository root
   - Activates environment automatically

3. **Dependencies**
   - Installs all packages from `requirements.txt`
   - Includes: pydantic, chromadb, langfuse, sentence-transformers, anthropic, mcp

4. **Environment Configuration**
   - Creates `.env` from `env.example`
   - Prompts for missing API keys

5. **ChromaDB Setup**
   - Initializes vector database for skills/implants
   - Auto-indexes on first MCP server run

6. **Validation**
   - Checks `mcp.json` configuration
   - Verifies `.cursor/` directory structure
   - Reports agent/skill/implant counts

## Flags

| Flag | Description |
|------|-------------|
| `--skip-env` | Skip `.env` file creation |
| `--skip-chroma` | Skip ChromaDB initialization check |
| `--help` | Show help message |

## Manual Alternative

If you prefer manual setup:

```bash
# 1. Create venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install deps
pip install -r requirements.txt

# 3. Configure environment
cp env.example .env
nano .env  # Add your API keys

# 4. Open in Cursor
cursor .
```

## Required API Keys

| Key | Purpose | Required |
|-----|---------|----------|
| `LANGFUSE_PUBLIC_KEY` | Observability tracing | Optional |
| `LANGFUSE_SECRET_KEY` | Observability tracing | Optional |
| `LANGFUSE_HOST` | Custom LangFuse instance | Optional |
| `ANTHROPIC_API_KEY` | Document OCR (Vision) | Optional |
