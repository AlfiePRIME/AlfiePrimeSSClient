"""AirPlay 2 receiver integration for AlfiePRIME Musiciser.

Wraps the vendored openairplay/airplay2-receiver to provide AirPlay audio
reception alongside (or instead of) the SendSpin receiver.  Decoded PCM
audio is routed to the shared AudioVisualizer and the PlayerState is
updated with metadata / artwork from AirPlay clients.

Optional – gracefully disabled when dependencies are missing.
"""
from __future__ import annotations

_HAS_AIRPLAY = False
_MISSING_REASON = ""

try:
    import av  # noqa: F401
    import pyaudio  # noqa: F401
    from Crypto.Cipher import ChaCha20_Poly1305  # noqa: F401
    import biplist  # noqa: F401
    import netifaces  # noqa: F401
    _HAS_AIRPLAY = True
except ImportError as exc:
    _MISSING_REASON = str(exc)
