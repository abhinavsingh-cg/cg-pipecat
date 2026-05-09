import re
from dataclasses import dataclass
from typing import List, Dict, Callable, Optional
from enum import Enum

from voicebot.indic_numtowords.numtowords import num2words

# =========================================================
# ENUMS
# =========================================================

class BucketType(str, Enum):
    PHONE_NUMBER = "phone_number"
    DATE = "date"
    AMOUNT = "amount"
    FLOAT = "float"
    REFERENCE_ID = "reference_id"
    RANDOM_DIGIT = "random_digit"
    OTHERS = "others"


# =========================================================
# DATA CLASS
# =========================================================

@dataclass
class TaggedSpan:
    start: int
    end: int
    original_text: str
    normalized_text: str
    bucket: BucketType


# =========================================================
# MAIN CLASS
# =========================================================

class UniversalTextNormalizer:
    """
    High-performance multilingual digit parser + bucket classifier.

    Flow:
        1. Normalize Indic digits -> English digits
        2. Extract digit-like spans
        3. Classify each span into bucket
        4. Route span to corresponding processor
        5. Replace processed text
        6. Return final text
    """

    # -----------------------------------------------------
    # INDIC DIGIT MAP
    # -----------------------------------------------------

    DIGIT_TRANSLATION = str.maketrans({
        # Hindi / Marathi
        "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
        "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",

        # Bengali
        "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4",
        "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9",

        # Gujarati
        "૦": "0", "૧": "1", "૨": "2", "૩": "3", "૪": "4",
        "૫": "5", "૬": "6", "૭": "7", "૮": "8", "૯": "9",

        # Punjabi
        "੦": "0", "੧": "1", "੨": "2", "੩": "3", "੪": "4",
        "੫": "5", "੬": "6", "੭": "7", "੮": "8", "੯": "9",

        # Tamil
        "௦": "0", "௧": "1", "௨": "2", "௩": "3", "௪": "4",
        "௫": "5", "௬": "6", "௭": "7", "௮": "8", "௯": "9",

        # Telugu
        "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4",
        "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",

        # Kannada
        "೦": "0", "೧": "1", "೨": "2", "೩": "3", "೪": "4",
        "೫": "5", "೬": "6", "೭": "7", "೮": "8", "೯": "9",

        # Malayalam
        "൦": "0", "൧": "1", "൨": "2", "൩": "3", "൪": "4",
        "൫": "5", "൬": "6", "൭": "7", "൮": "8", "൯": "9",
    })

    # -----------------------------------------------------
    # FAST REGEX COMPILED ONCE
    # -----------------------------------------------------

    PHONE_REGEX = re.compile(
        r'(?<!\d)(?:(?:\+91[\s-]?)?[6-9]\d{9}|0[6-9]\d{9}|(?:\+91[\s-]?)?[6-9]\d{4}[\s-]\d{5}|(?:\+91[\s-]?)?\([6-9]\d{2}\)[\s-]?\d{3}[\s-]?\d{4}|0\d{2,4}[\s-]\d{6,8}|\d{3,5}[\s-]\d{6,8}|1(?:800|860)[-\s]?\d{3,4}[-\s]?\d{4})(?!\d)'
    )


    DATE_REGEX = re.compile(
        r'(?<!\d)(?:(?:0?[1-9]|[12]\d|3[01])[-\/.](?:0?[1-9]|1[0-2])[-\/.](?:\d{2}|\d{4})|\d{4}[-\/](?:0?[1-9]|1[0-2])[-\/](?:0?[1-9]|[12]\d|3[01]))(?!\d)'
    )

    AMOUNT_REGEX = re.compile(
        r'(?<!\w)(?:₹|\$|rs\.?|inr)?\s?\d+(?:,\d+)*(?:\.\d+)?(?!\w)',
        re.IGNORECASE
    )

    FLOAT_REGEX = re.compile(
        r'(?<!\w)\d+\.\d+(?!\w)'
    )

    REFERENCE_REGEX = re.compile(
        r'(?<!\w)[A-Za-z]*\d{5,}[A-Za-z\d]*(?!\w)'
    )

    RANDOM_DIGIT_REGEX = re.compile(
        r'(?<!\w)\d+(?!\w)'
    )

    # Generic extraction regex
    EXTRACTION_REGEX = re.compile(
        r'[\w@#:/\-.+]?\d[\w@#:/\-.+]*'
    )

    # -----------------------------------------------------
    # INIT
    # -----------------------------------------------------

    def __init__(self):

        self.bucket_processors: Dict[
            BucketType,
            Callable[[str], str]
        ] = {
            BucketType.PHONE_NUMBER: self.process_phone_number,
            BucketType.DATE: self.process_date,
            BucketType.AMOUNT: self.process_amount,
            BucketType.FLOAT: self.process_float,
            BucketType.REFERENCE_ID: self.process_reference_id,
            BucketType.RANDOM_DIGIT: self.process_random_digit,
            BucketType.OTHERS: self.process_others,
        }

    # =====================================================
    # STEP 1: DIGIT NORMALIZATION
    # =====================================================

    def normalize_indic_digits(self, text: str) -> str:
        """
        Extremely fast unicode digit normalization.
        Uses native C-level str.translate().
        """
        return text.translate(self.DIGIT_TRANSLATION)

    # =====================================================
    # STEP 2: CLASSIFICATION
    # =====================================================

    def classify(self, token: str) -> BucketType:
        if self.PHONE_REGEX.fullmatch(token):
            return BucketType.PHONE_NUMBER

        if self.DATE_REGEX.fullmatch(token):
            return BucketType.DATE

        if self.AMOUNT_REGEX.fullmatch(token):
            return BucketType.AMOUNT

        if self.FLOAT_REGEX.fullmatch(token):
            return BucketType.FLOAT

        if self.REFERENCE_REGEX.fullmatch(token):
            return BucketType.REFERENCE_ID

        if self.RANDOM_DIGIT_REGEX.fullmatch(token):
            return BucketType.RANDOM_DIGIT
        
        return BucketType.OTHERS

    # =====================================================
    # STEP 3: EXTRACT + TAG
    # =====================================================

    def extract_and_tag(self, text: str) -> List[TaggedSpan]:

        tagged_items = []

        for match in self.EXTRACTION_REGEX.finditer(text):

            token = match.group()

            bucket = self.classify(token)

            tagged_items.append(
                TaggedSpan(
                    start=match.start(),
                    end=match.end(),
                    original_text=token,
                    normalized_text=token,
                    bucket=bucket
                )
            )
        return tagged_items

    # =====================================================
    # STEP 4: PROCESS
    # =====================================================

    def process(self, text: str) -> str:

        # ---------------------------------------------
        # normalize indic digits first
        # ---------------------------------------------
        normalized_text = self.normalize_indic_digits(text)

        # ---------------------------------------------
        # extract spans
        # ---------------------------------------------
        tagged_spans = self.extract_and_tag(normalized_text)

        if not tagged_spans:
            return normalized_text

        # ---------------------------------------------
        # replace safely using chunk building
        # fastest approach
        # ---------------------------------------------
        final_chunks = []

        last_idx = 0

        for span in tagged_spans:

            # append untouched text
            final_chunks.append(
                normalized_text[last_idx:span.start]
            )

            # process current span
            processor = self.bucket_processors[span.bucket]

            processed_value = processor(span.normalized_text)

            final_chunks.append(processed_value)

            last_idx = span.end

        # append remaining text
        final_chunks.append(normalized_text[last_idx:])

        return "".join(final_chunks)

    # =====================================================
    # BUCKET PROCESSORS
    # =====================================================

    def process_phone_number(self, text: str) -> str:
        """
        Future:
            - digit splitting
            - TTS friendly
            - masking
            - etc
        """
        return text

    def process_date(self, text: str) -> str:
        return text

    def process_amount(self, text: str) -> str:
        # Remove commas
        text = text.replace(",", "")
        
        # Handle decimal part if it exists
        if "." in text:
            integer_part, decimal_part = text.split(".", 1)
            # Add spaces between decimal digits for TTS (e.g. "50" -> "5 0")
            spaced_decimals = " ".join(list(decimal_part))
            return f"{integer_part}  {spaced_decimals}"
            
        return text

    def process_float(self, text: str) -> str:
        if "." in text:
            integer_part, decimal_part = text.split(".", 1)
            # Add spaces between decimal digits for TTS (e.g. "75" -> "7 5")
            spaced_decimals = " ".join(list(decimal_part))
            return f"{integer_part}  {spaced_decimals}"
        return text

    def process_reference_id(self, text: str) -> str:
        # Space out every character so the TTS spells it out individually (e.g. AB123 -> A B 1 2 3)
        return " ".join(list(text.upper()))

    def process_random_digit(self, text: str) -> str:
        # Separate digits by spaces so TTS spells them individually (e.g. 1234 -> 1 2 3 4)
        return " ".join(list(text))

    def process_others(self, text: str) -> str:
        # Separate unrecognized combinations by spaces to force spelling
        return " ".join(list(text))

    

 
# =========================================================
# PIPECAT FRAME PROCESSOR INTEGRATION
# =========================================================

import logging
from pipecat.frames.frames import (
    Frame,
    TextFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from voicebot.config import LANGUAGE_TO_CODES

logger = logging.getLogger(__name__)

class DigitHandlingProcessor(FrameProcessor):
    def __init__(self, lang: str = "en", state=None):
        super().__init__()
        self.normalizer = UniversalTextNormalizer()
        self._pending = ""
        self._lang = lang
        self._state = state

    def _resolve_num2words_lang(self) -> Optional[str]:
        if self._state is None:
            return self._lang
        codes = LANGUAGE_TO_CODES.get(self._state.current_language)
        return codes if codes else self._lang

    def convert_text_numbers_to_words(self, text: str, lang: str = "en") -> str:
        """Replace every number in *text* with its word form in *lang*.

        Scans the input string for digit sequences (including optional commas
        like 1,00,000) and substitutes each with the output of ``num2words``.

        Args:
            text: A string that may contain embedded numbers.
            lang: Target language code (e.g. "hi", "ta", "en").

        Returns:
            The string with every number replaced by its word representation.

        Example:
            >>> convert_text("I have 500 rupees and 20 coins", lang="hi")
            'I have पाँच सौ rupees and बीस coins'
        """
        def _replace(match: re.Match) -> str:
            raw = match.group(0)
            try:
                num = int(raw.replace(",", ""))
                return num2words(num, lang=lang)
            except Exception as e:
                logger.error(f"Error converting number {raw} to words: {e}")
                return raw

        # Matches digit groups possibly separated by commas (Indian / Western)
        return re.sub(r"\d[\d,]*", _replace, text)

    async def _flush_pending(self, direction: FrameDirection):
        if not self._pending:
            return
        
        # Process the full accumulated string
        logger.info(f"digit_handler_flush | before: {self._pending!r}")
        processed = self.normalizer.process(self._pending)
        current_lang = self._resolve_num2words_lang()
        processed = self.convert_text_numbers_to_words(processed, lang=current_lang)
        logger.info(f"digit_handler_flush | after: {processed!r}")
        
        if processed:
            await self.push_frame(TextFrame(text=processed), direction)
        self._pending = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Drop stale text on new response
        if isinstance(frame, LLMFullResponseStartFrame):
            self._pending = ""
            await self.push_frame(frame, direction)
            return

        # Flush on response end
        if isinstance(frame, LLMFullResponseEndFrame):
            await self._flush_pending(direction)
            await self.push_frame(frame, direction)
            return

        # Drop text on interruption
        if isinstance(frame, InterruptionFrame):
            self._pending = ""
            await self.push_frame(frame, direction)
            return

        # Buffer streaming text frames and process sentence by sentence
        if isinstance(frame, TextFrame) and frame.text:
            self._pending += frame.text
            
            # Split on sentence boundaries (. ! ? followed by space or end)
            parts = re.split(r'([.!?]+(?:\s+|$))', self._pending)
            
            if len(parts) > 1:
                # The last element is the incomplete trailing part
                self._pending = parts.pop()
                
                # Rejoin the complete sentences
                complete_text = "".join(parts)
                if complete_text.strip():
                    logger.info(f"digit_handler_chunk | before: {complete_text!r}")
                    processed = self.normalizer.process(complete_text)
                    current_lang = self._resolve_num2words_lang()
                    processed = self.convert_text_numbers_to_words(processed, lang=current_lang)
                    logger.info(f"digit_handler_chunk | after: {processed!r}")
                    await self.push_frame(TextFrame(text=processed), direction)
            
            return

        # Pass through all other frames (audio, etc.)
        await self.push_frame(frame, direction)