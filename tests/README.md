# Tests

Unit tests for the Agents framework.

## Running Tests

### Quick Start

```bash
# Run all language detection tests
./scripts/run_tests.sh

# Run with additional pytest flags
./scripts/run_tests.sh -v --tb=short
./scripts/run_tests.sh -k "test_russian"
```

### Manual Execution

```bash
# Using pyenv Python
~/.pyenv/versions/3.12.4/bin/python -m pytest tests/test_language.py -v

# Or activate virtual environment
source .venv/bin/activate
pytest tests/test_language.py -v
```

## Test Coverage

### Language Detection (`test_language.py`)

**29 tests** covering:

| Category | Tests | Description |
|----------|-------|-------------|
| **Detection** | 6 | Russian, English, German, Spanish, French, Arabic |
| **Edge Cases** | 7 | Empty strings, whitespace, numbers, symbols, short text |
| **Functionality** | 5 | Confidence scores, caching, singleton pattern |
| **Error Handling** | 4 | Exceptions, missing library, unmapped codes |
| **Validation** | 7 | Language mapping, Unicode, mixed scripts |

### Expected Results

All 29 tests should pass:

```
============================= test session starts ==============================
platform linux -- Python 3.12.4, pytest-9.0.2, pluggy-1.6.0
collected 29 items

tests/test_language.py::TestLanguageDetector::test_detector_initialization PASSED
tests/test_language.py::TestLanguageDetector::test_russian_detection PASSED
...
tests/test_language.py::TestEdgeCases::test_special_characters_with_text PASSED

============================== 29 passed in 0.37s ===============================
```

## Dependencies

Required packages (from `requirements.txt`):
- `langdetect>=1.0.9` - Language detection library
- `pytest>=7.0.0` - Testing framework

## Test Environment

The test suite requires:
1. Python 3.10+ (3.12.4 recommended)
2. `langdetect` library installed
3. All dependencies from `requirements.txt`

### Environment Detection

The `run_tests.sh` script automatically detects:
1. `.venv/` or `venv/` virtual environment
2. pyenv Python installation (prefers 3.12.4)
3. Validates required dependencies

## Adding New Tests

1. Create test file in `tests/` directory
2. Follow naming convention: `test_*.py`
3. Use pytest fixtures and assertions
4. Run `./scripts/run_tests.sh` to verify

Example:

```python
def test_new_feature():
    """Test description."""
    # Arrange
    detector = LanguageDetector()
    
    # Act
    result = detector.detect("Some text")
    
    # Assert
    assert result == "English"
```
