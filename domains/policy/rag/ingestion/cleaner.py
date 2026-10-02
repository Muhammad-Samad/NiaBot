"""
Text normalisation applied before chunking.

PDF extraction leaves trailing spaces, odd bullet glyphs and "space-only"
lines. We normalise these so the chunker can rely on simple line patterns:
  - "•" bullets       -> "- "
  - "o" sub-bullets   -> "  - "
  - blank/space lines -> ""   (paragraph break)
"""
from __future__ import annotations

import re
import unicodedata

_BULLET_RE = re.compile(r"^\s*[•●▪■◦·]\s*")
_SUB_BULLET_RE = re.compile(r"^\s*o\s+(?=\S)")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t ]{2,}")


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")

    out: list[str] = []
    for raw in text.split("\n"):
        line = _MULTI_SPACE_RE.sub(" ", raw).rstrip()
        if not line.strip():
            out.append("")
            continue
        if _BULLET_RE.match(line):
            line = "- " + _BULLET_RE.sub("", line)
        elif _SUB_BULLET_RE.match(line):
            line = "  - " + _SUB_BULLET_RE.sub("", line)
        else:
            line = line.strip()
        out.append(line)

    return _MULTI_BLANK_RE.sub("\n\n", "\n".join(out)).strip()
