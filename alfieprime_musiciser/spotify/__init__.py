"""Spotify Connect receiver integration for AlfiePRIME Musiciser.

Wraps librespot to provide Spotify Connect audio reception alongside
other sources.  Metadata and transport controls use the Spotify Web API
via spotipy.

Optional – gracefully disabled when dependencies are missing.
"""
from __future__ import annotations

import shutil

_HAS_SPOTIFY = False
_MISSING_REASON = ""
_MISSING_DEPS: list[str] = []

# Check for librespot binary
if not shutil.which("librespot"):
    _MISSING_DEPS.append("librespot binary (pacman -S librespot / cargo install librespot)")

# Check for spotipy Python package
try:
    import spotipy  # noqa: F401
except ImportError:
    _MISSING_DEPS.append("spotipy (pip install 'spotipy>=2.23.0')")

if _MISSING_DEPS:
    _MISSING_REASON = "Missing: " + ", ".join(_MISSING_DEPS)
else:
    _HAS_SPOTIFY = True
