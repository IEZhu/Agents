import logging
from typing import Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

# Default language when detection fails or is ambiguous
DEFAULT_LANGUAGE = "English"

# ISO 639-1 code to full language name mapping
LANG_MAP = {
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "ja": "Japanese",
    "ko": "Korean",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "ar": "Arabic",
    "hi": "Hindi",
    "uk": "Ukrainian",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "el": "Greek",
    "he": "Hebrew",
}


class LanguageDetector:
    """
    Detects the language of a given text using langdetect library.
    Returns human-readable language names (e.g., "Russian", "English").
    Falls back to English on detection failure or unmapped codes.
    """

    def __init__(self):
        """Initialize the LanguageDetector."""
        try:
            # Import langdetect only when needed to avoid import errors if not installed
            import langdetect
            self.langdetect = langdetect
            # Set seed for consistent results across runs
            langdetect.DetectorFactory.seed = 0
        except ImportError:
            logger.error("langdetect library not installed. Language detection will always return default.")
            self.langdetect = None

    @lru_cache(maxsize=1024)
    def detect(self, text: str) -> str:
        """
        Detects the language of the provided text.

        Args:
            text: The text to analyze.

        Returns:
            Full language name (e.g., "English", "Russian").
            Returns DEFAULT_LANGUAGE if detection fails.
        """
        if not self.langdetect:
            logger.warning("langdetect not available, using default language")
            return DEFAULT_LANGUAGE

        # Handle edge cases
        if not text or not text.strip():
            logger.debug("Empty text provided, using default language")
            return DEFAULT_LANGUAGE

        # Remove whitespace and check if text has enough substance
        cleaned_text = text.strip()
        if len(cleaned_text) < 3:
            logger.debug(f"Text too short for reliable detection: '{cleaned_text}', using default")
            return DEFAULT_LANGUAGE

        try:
            # Detect ISO code
            iso_code = self.langdetect.detect(cleaned_text)
            logger.debug(f"Detected ISO code: {iso_code}")

            # Map to full language name
            language_name = LANG_MAP.get(iso_code, None)

            if language_name:
                logger.info(f"Detected language: {language_name} (ISO: {iso_code})")
                return language_name
            else:
                logger.warning(f"Unmapped ISO code '{iso_code}', using default language")
                return DEFAULT_LANGUAGE

        except self.langdetect.LangDetectException as e:
            logger.warning(f"Language detection failed for text '{cleaned_text[:50]}...': {e}")
            return DEFAULT_LANGUAGE
        except Exception as e:
            logger.error(f"Unexpected error during language detection: {e}")
            return DEFAULT_LANGUAGE

    def detect_with_confidence(self, text: str) -> tuple[str, float]:
        """
        Detects language with confidence score.

        Args:
            text: The text to analyze.

        Returns:
            Tuple of (language_name, confidence_score).
            Confidence is 0.0 if detection fails.
        """
        if not self.langdetect:
            return DEFAULT_LANGUAGE, 0.0

        if not text or not text.strip():
            return DEFAULT_LANGUAGE, 0.0

        try:
            # Get probabilities for all detected languages
            probabilities = self.langdetect.detect_langs(text)

            if not probabilities:
                return DEFAULT_LANGUAGE, 0.0

            # Get the top result
            top_result = probabilities[0]
            iso_code = top_result.lang
            confidence = top_result.prob

            language_name = LANG_MAP.get(iso_code, DEFAULT_LANGUAGE)
            logger.info(f"Detected: {language_name} with confidence {confidence:.2f}")

            return language_name, confidence

        except Exception as e:
            logger.error(f"Error in detect_with_confidence: {e}")
            return DEFAULT_LANGUAGE, 0.0


# Global singleton instance
_detector_instance: Optional[LanguageDetector] = None


def get_detector() -> LanguageDetector:
    """
    Returns a singleton instance of LanguageDetector.
    Lazy initialization to avoid import errors during module load.
    """
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = LanguageDetector()
    return _detector_instance


def detect_language(text: str) -> str:
    """
    Convenience function to detect language from text.

    Args:
        text: The text to analyze.

    Returns:
        Full language name (e.g., "English", "Russian").
    """
    detector = get_detector()
    return detector.detect(text)
