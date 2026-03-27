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
_MISSING_DEPS: list[str] = []

# Check each dependency individually so we can report all missing ones
_AIRPLAY_DEPS = {
    "av": "av (pip install av / pacman -S python-av)",
    "pyaudio": "pyaudio (pip install pyaudio / pacman -S python-pyaudio)",
    "Crypto": "pycryptodome (pip install pycryptodome / pacman -S python-pycryptodome)",
    "biplist": "biplist (pip install biplist)",
    "netifaces": "netifaces (pip install netifaces / pacman -S python-netifaces)",
    "srptools": "srptools (pip install srptools)",
    "hkdf": "hkdf (pip install hkdf)",
    "cryptography": "cryptography (pip install cryptography / pacman -S python-cryptography)",
}

for _mod, _desc in _AIRPLAY_DEPS.items():
    try:
        __import__(_mod)
    except ImportError:
        _MISSING_DEPS.append(_desc)

if _MISSING_DEPS:
    _MISSING_REASON = "Missing: " + ", ".join(_MISSING_DEPS)
else:
    _HAS_AIRPLAY = True
