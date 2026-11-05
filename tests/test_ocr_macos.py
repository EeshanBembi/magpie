"""Optional real macOS OCR check.

Skipped by default.  Run with ``MAGPIE_TEST_OCR=1`` after building the helper
to verify Vision can read a Pillow-rendered image:

    MAGPIE_TEST_OCR=1 .venv/bin/python -m pytest -q -m macos
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS Vision only"),
    pytest.mark.skipif(
        os.environ.get("MAGPIE_TEST_OCR") != "1",
        reason="set MAGPIE_TEST_OCR=1 to run the real OCR test",
    ),
]


def test_ocr_reads_rendered_text(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from magpie import helper

    image = Image.new("RGB", (1200, 300), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 96)
            break
        except OSError:
            continue
    if font is None:
        pytest.skip("no usable system font")
    draw.text((40, 80), "Hello Magpie", fill="black", font=font)
    path = tmp_path / "hello.png"
    image.save(path)

    result = helper.ocr(path)
    assert "magpie" in result["text"].lower()
    assert result["lines"]
