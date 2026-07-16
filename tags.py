"""Release-label detection for admin link output only; no user-side quality buttons."""
from __future__ import annotations

import re

_RESOLUTION = re.compile(r"(?i)(?<![a-z0-9])(2160p|4k|1440p|1080p|720p|480p|360p|240p|uhd|fhd)(?![a-z0-9])")
_SOURCES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(?<![a-z0-9])web[ ._-]?dl(?![a-z0-9])"), "WEB-DL"),
    (re.compile(r"(?i)(?<![a-z0-9])web[ ._-]?rip(?![a-z0-9])"), "WEBRip"),
    (re.compile(r"(?i)(?<![a-z0-9])hdrip(?![a-z0-9])"), "HDRip"),
    (re.compile(r"(?i)(?<![a-z0-9])blu[ ._-]?ray(?![a-z0-9])"), "BluRay"),
    (re.compile(r"(?i)(?<![a-z0-9])bd[ ._-]?rip(?![a-z0-9])"), "BDRip"),
    (re.compile(r"(?i)(?<![a-z0-9])remux(?![a-z0-9])"), "REMUX"),
    (re.compile(r"(?i)(?<![a-z0-9])dvd[ ._-]?rip(?![a-z0-9])"), "DVDRip"),
    (re.compile(r"(?i)(?<![a-z0-9])hdtv(?![a-z0-9])"), "HDTV"),
    (re.compile(r"(?i)(?<![a-z0-9])hdcam(?![a-z0-9])"), "HDCAM"),
    (re.compile(r"(?i)(?<![a-z0-9])cam(?![a-z0-9])"), "CAM"),
]
_CODECS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(?<![a-z0-9])hevc(?![a-z0-9])"), "HEVC"),
    (re.compile(r"(?i)(?<![a-z0-9])x265(?![a-z0-9])"), "x265"),
    (re.compile(r"(?i)(?<![a-z0-9])x264(?![a-z0-9])"), "x264"),
    (re.compile(r"(?i)(?<![a-z0-9])10[ ._-]?bit(?![a-z0-9])"), "10Bit"),
]


def extract_tag(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    parts: list[str] = []
    match = _RESOLUTION.search(stem)
    if match:
        raw = match.group(1).upper()
        parts.append({"2160P": "4K", "4K": "4K", "UHD": "4K", "FHD": "1080p"}.get(raw, raw.lower().replace("P", "p")))
    for pattern, label in _SOURCES:
        if pattern.search(stem):
            parts.append(label)
            break
    if len(parts) <= 1:
        for pattern, label in _CODECS:
            if pattern.search(stem):
                parts.append(label)
                break
    return " ".join(parts) or "File"
