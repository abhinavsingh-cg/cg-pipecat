"""
Smoke tests for TextNormalizationProcessor.

Run:
    cd /Users/admin/cg-pipecat
    python -m voicebot.tests.test_text_normalizer
    # or with pytest:
    pytest voicebot/tests/test_text_normalizer.py -v

Covers:
  1. _replace_numbers — pure function, no Pipecat dependency
       • plain integers
       • thousands-separated (10,000  →  ten thousand)
       • decimals  (12.75  →  twelve point seven five)
       • mixed     (2,026.50)
       • non-number commas/periods left untouched
  2. TextNormalizationProcessor — streaming buffer
       • holds across digit chunks       ("202","6"," से")  → one number
       • holds across thousands separator ("10",",","000")  → one number
       • holds across decimal point       ("12",".","75")    → "twelve point seven five"
       • flushes on LLMFullResponseEndFrame
       • drops pending on InterruptionFrame
"""
from __future__ import annotations

import asyncio
import sys
import types

from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.processors.text_normalizer import (
    TextNormalizationProcessor,
    _replace_numbers,
)


# ── pure-function tests ──────────────────────────────────────────────────────

def test_replace_numbers_pure() -> None:
    cases = [
        # input,                               expected
        ("12345",                              "twelve thousand, three hundred and forty-five"),
        ("10,000",                             "ten thousand"),
        ("2,026",                              "two thousand and twenty-six"),
        ("12.75",                              "twelve point seven five"),
        ("2,026.50",                           "two thousand and twenty-six point five zero"),
        ("Hi, there",                          "Hi, there"),                  # comma is punctuation
        ("12. Hello",                          "twelve. Hello"),               # dot is sentence end
        ("EMI is 10,000 since 28 April 2026,", "EMI is ten thousand since twenty-eight April two thousand and twenty-six,"),
        ("",                                   ""),
    ]
    for raw, expected in cases:
        got = _replace_numbers(raw, lang="en")
        print(got)
        assert got == expected, f"\n  input:    {raw!r}\n  expected: {expected!r}\n  got:      {got!r}"
    print("✓ _replace_numbers pure function")


# ── streaming-buffer tests ───────────────────────────────────────────────────

class _Harness:
    """
    Drives a TextNormalizationProcessor without a real pipeline.

    Patches FrameProcessor.process_frame at the class level to skip the
    setup-dependent bookkeeping (observer/clock) so we can call the subclass
    process_frame directly. Captures every frame the processor pushes.
    """

    def __init__(self) -> None:
        self.pushed: list = []
        self.proc = TextNormalizationProcessor(lang="en")

        async def _noop_super(self_, frame, direction):  # noqa: ANN001
            return None

        async def _capture(frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        # Bypass the FrameProcessor base bookkeeping (it needs a pipeline).
        FrameProcessor.process_frame = _noop_super  # type: ignore[assignment]
        self.proc.push_frame = _capture  # type: ignore[assignment]

    async def feed(self, *chunks: str) -> None:
        for c in chunks:
            await self.proc.process_frame(TextFrame(text=c), FrameDirection.DOWNSTREAM)

    async def end(self) -> None:
        await self.proc.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    async def start(self) -> None:
        await self.proc.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)

    async def interrupt(self) -> None:
        await self.proc.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    def emitted_text(self) -> str:
        return "".join(f.text for f in self.pushed if isinstance(f, TextFrame))


async def _run_streaming_tests() -> None:
    # Case 1: digit run split mid-number ("202" + "6" + " से")
    h = _Harness()
    await h.feed("202", "6", " से")
    await h.end()
    out = h.emitted_text()
    assert "two thousand and twenty-six" in out, out
    assert "two hundred" not in out, out
    print(out)
    print("✓ digit-split: 202 + 6 → two thousand and twenty-six")

    # Case 2: thousands separator across chunks ("10", ",", "000", " pending")
    h = _Harness()
    await h.feed("Your EMI is ", "10", ",", "000", " pending")
    await h.end()
    out = h.emitted_text()
    assert "ten thousand" in out, out
    assert "ten,"  not in out, out
    print(out)
    print("✓ thousands-sep split: 10 + , + 000 → ten thousand")

    # Case 3: decimal across chunks ("12", ".", "75", " kg")
    h = _Harness()
    await h.feed("Weight ", "12", ".", "75", " kg")
    await h.end()
    out = h.emitted_text()
    assert "twelve point seven five" in out, out
    print(out)
    print("✓ decimal split: 12 + . + 75 → twelve point seven five")

    # Case 4: number lands exactly on stream end (pending must flush)
    h = _Harness()
    await h.feed("The answer is ", "42")
    await h.end()
    out = h.emitted_text()
    assert "forty-two" in out, out
    print(out)
    print("✓ pending flushed on LLMFullResponseEndFrame")

    # Case 5: InterruptionFrame drops pending so it doesn't bleed into next turn
    h = _Harness()
    await h.feed("Cost is ", "10", ",")
    await h.interrupt()
    await h.start()
    await h.feed("New answer ", "5", " done")
    await h.end()
    out = h.emitted_text()
    assert "ten" not in out, f"stale pending leaked: {out!r}"
    assert "five" in out, out
    print(out)
    print("✓ InterruptionFrame drops stale pending")

    # Case 6: punctuation comma (not number) emits without delay
    h = _Harness()
    await h.feed("Hello", ", ", "world")
    await h.end()
    out = h.emitted_text()
    assert out == "Hello, world", out
    print(out)
    print("✓ non-number comma passes through unchanged")

    # Case 7: large mixed example mirroring real LLM output
    h = _Harness()
    await h.feed(
        "Your", " loan", "'s", " EMI", " is", " ₹", "10", ",", "000",
        ",", " pending", " since", " ", "28", " April", " ", "202", "6", ".",
    )
    await h.end()
    out = h.emitted_text()
    assert "ten thousand" in out, out
    assert "twenty-eight" in out, out
    assert "two thousand and twenty-six" in out, out
    print(out)
    print(f"✓ end-to-end mixed: {out!r}")


# ── runner ───────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        test_replace_numbers_pure()
        asyncio.run(_run_streaming_tests())
    except AssertionError as exc:
        print(f"✗ FAIL: {exc}")
        return 1
    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
