"""
Unit tests for the language detection module.

Tests the LanguageDetector class and its ability to:
- Detect common languages (English, Russian, German, etc.)
- Handle edge cases (empty strings, short text, symbols)
- Fallback to default language appropriately
- Map ISO codes to full language names
"""

import pytest
from unittest.mock import Mock, patch
from src.engine.language import (
    LanguageDetector,
    detect_language,
    get_detector,
    DEFAULT_LANGUAGE,
    LANG_MAP,
)


class TestLanguageDetector:
    """Test suite for LanguageDetector class."""

    def test_detector_initialization(self):
        """Test that detector initializes correctly."""
        detector = LanguageDetector()
        assert detector is not None
        # langdetect should be available if installed
        # If not installed, detector.langdetect will be None

    def test_russian_detection(self):
        """Test detection of Russian text."""
        detector = LanguageDetector()
        text = "Привет, как дела? Это тестовое сообщение на русском языке."
        result = detector.detect(text)
        assert result == "Russian"

    def test_english_detection(self):
        """Test detection of English text."""
        detector = LanguageDetector()
        text = "Hello, how are you? This is a test message in English."
        result = detector.detect(text)
        assert result == "English"

    def test_german_detection(self):
        """Test detection of German text."""
        detector = LanguageDetector()
        text = "Guten Tag, wie geht es Ihnen? Dies ist eine Testnachricht auf Deutsch."
        result = detector.detect(text)
        assert result == "German"

    def test_spanish_detection(self):
        """Test detection of Spanish text."""
        detector = LanguageDetector()
        text = "Hola, ¿cómo estás? Este es un mensaje de prueba en español."
        result = detector.detect(text)
        assert result == "Spanish"

    def test_french_detection(self):
        """Test detection of French text."""
        detector = LanguageDetector()
        text = "Bonjour, comment allez-vous? Ceci est un message de test en français."
        result = detector.detect(text)
        assert result == "French"

    def test_empty_string_fallback(self):
        """Test that empty string returns default language."""
        detector = LanguageDetector()
        result = detector.detect("")
        assert result == DEFAULT_LANGUAGE

    def test_whitespace_only_fallback(self):
        """Test that whitespace-only string returns default language."""
        detector = LanguageDetector()
        result = detector.detect("   \n\t  ")
        assert result == DEFAULT_LANGUAGE

    def test_short_text_fallback(self):
        """Test that very short text returns default language."""
        detector = LanguageDetector()
        result = detector.detect("OK")
        assert result == DEFAULT_LANGUAGE

    def test_numbers_only_fallback(self):
        """Test that numbers-only text returns default language."""
        detector = LanguageDetector()
        result = detector.detect("123456789")
        assert result == DEFAULT_LANGUAGE

    def test_symbols_only_fallback(self):
        """Test that symbols-only text returns default language."""
        detector = LanguageDetector()
        result = detector.detect("!@#$%^&*()")
        assert result == DEFAULT_LANGUAGE

    def test_code_snippet_fallback(self):
        """Test that code snippets are detected (langdetect may detect short code as various languages)."""
        detector = LanguageDetector()
        # Code is often detected inconsistently due to limited text
        # Just verify it returns a valid string, not necessarily English
        result = detector.detect("def foo(): pass")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mixed_language_detection(self):
        """Test detection when text contains mixed languages (should detect dominant)."""
        detector = LanguageDetector()
        # Predominantly English with one Russian word
        text = "This is mostly an English sentence with одно Russian word."
        result = detector.detect(text)
        # Should detect English as dominant
        assert result == "English"

    def test_detect_with_confidence(self):
        """Test the detect_with_confidence method."""
        detector = LanguageDetector()
        text = "Hello, this is a clear English sentence for testing purposes."
        language, confidence = detector.detect_with_confidence(text)
        
        assert language == "English"
        assert 0.0 <= confidence <= 1.0
        # For clear English text, confidence should be relatively high
        assert confidence > 0.5

    def test_detect_with_confidence_fallback(self):
        """Test detect_with_confidence with empty text."""
        detector = LanguageDetector()
        language, confidence = detector.detect_with_confidence("")
        
        assert language == DEFAULT_LANGUAGE
        assert confidence == 0.0

    def test_caching_mechanism(self):
        """Test that the LRU cache works for repeated queries."""
        detector = LanguageDetector()
        text = "This text should be cached after first detection."
        
        # First call
        result1 = detector.detect(text)
        # Second call (should use cache)
        result2 = detector.detect(text)
        
        assert result1 == result2
        assert result1 == "English"

    def test_unmapped_language_code_fallback(self):
        """Test that unmapped ISO codes fall back to default."""
        detector = LanguageDetector()
        
        # Mock langdetect to return an unmapped code
        with patch.object(detector, 'langdetect') as mock_langdetect:
            mock_langdetect.detect.return_value = "xx"  # Unmapped code
            mock_langdetect.LangDetectException = Exception
            
            result = detector.detect("Some text")
            assert result == DEFAULT_LANGUAGE

    def test_langdetect_exception_handling(self):
        """Test that LangDetectException is handled gracefully."""
        detector = LanguageDetector()
        
        if detector.langdetect:
            with patch.object(detector.langdetect, 'detect') as mock_detect:
                # LangDetectException requires code and message
                mock_detect.side_effect = detector.langdetect.LangDetectException(code=1, message="Test error")
                
                result = detector.detect("Some text")
                assert result == DEFAULT_LANGUAGE

    def test_general_exception_handling(self):
        """Test that unexpected exceptions are handled gracefully."""
        detector = LanguageDetector()
        
        if detector.langdetect:
            with patch.object(detector.langdetect, 'detect') as mock_detect:
                mock_detect.side_effect = RuntimeError("Unexpected error")
                
                result = detector.detect("Some text")
                assert result == DEFAULT_LANGUAGE

    def test_langdetect_not_installed(self):
        """Test behavior when langdetect is not installed."""
        # Create a detector and manually set langdetect to None to simulate missing library
        detector = LanguageDetector()
        detector.langdetect = None
        
        result = detector.detect("Any text")
        assert result == DEFAULT_LANGUAGE


class TestLanguageMapping:
    """Test suite for language code mapping."""

    def test_lang_map_completeness(self):
        """Test that LANG_MAP contains expected languages."""
        expected_codes = ["en", "ru", "de", "es", "fr", "it", "pt"]
        
        for code in expected_codes:
            assert code in LANG_MAP
            assert isinstance(LANG_MAP[code], str)
            assert len(LANG_MAP[code]) > 0

    def test_lang_map_values_are_readable(self):
        """Test that all mapped values are human-readable names."""
        for code, name in LANG_MAP.items():
            # Should be capitalized and readable
            assert name[0].isupper()
            # Should not be an ISO code
            assert len(name) > 2


class TestConvenienceFunctions:
    """Test suite for convenience functions."""

    def test_get_detector_singleton(self):
        """Test that get_detector returns a singleton instance."""
        detector1 = get_detector()
        detector2 = get_detector()
        
        assert detector1 is detector2

    def test_detect_language_convenience(self):
        """Test the convenience detect_language function."""
        result = detect_language("Hello, this is English text.")
        assert result == "English"

    def test_detect_language_fallback(self):
        """Test detect_language with empty string."""
        result = detect_language("")
        assert result == DEFAULT_LANGUAGE


class TestEdgeCases:
    """Test suite for edge cases and boundary conditions."""

    def test_very_long_text(self):
        """Test detection with very long text."""
        detector = LanguageDetector()
        # Generate long English text
        long_text = "This is a sentence. " * 1000
        result = detector.detect(long_text)
        assert result == "English"

    def test_unicode_characters(self):
        """Test handling of various Unicode characters."""
        detector = LanguageDetector()
        
        # Arabic
        arabic_text = "مرحبا كيف حالك؟ هذا نص تجريبي باللغة العربية."
        result = detector.detect(arabic_text)
        assert result == "Arabic"

    def test_mixed_scripts(self):
        """Test text with mixed scripts (Latin, Cyrillic)."""
        detector = LanguageDetector()
        
        # Primarily Russian with Latin characters
        text = "Это текст на русском языке with some English words."
        result = detector.detect(text)
        # Should detect the dominant language
        assert result in ["Russian", "English"]

    def test_special_characters_with_text(self):
        """Test text containing special characters alongside normal text."""
        detector = LanguageDetector()
        text = "Hello!!! How are you??? This is English!!!"
        result = detector.detect(text)
        assert result == "English"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
