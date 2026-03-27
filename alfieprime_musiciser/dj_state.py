"""DJ mixing console state."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelState:
    """Per-channel mixer state."""

    volume: int = 100       # 0-100
    eq_bass: int = 0        # -12 to +12 dB
    eq_mid: int = 0         # -12 to +12 dB
    eq_treble: int = 0      # -12 to +12 dB


@dataclass
class DJState:
    """State for the DJ mixing console."""

    channel_a: ChannelState = field(default_factory=ChannelState)  # SendSpin
    channel_b: ChannelState = field(default_factory=ChannelState)  # AirPlay
    crossfader: float = 0.5  # 0.0 = full A, 1.0 = full B
    active_channel: str = "a"  # which channel has keyboard focus

    def get_focused(self) -> ChannelState:
        return self.channel_a if self.active_channel == "a" else self.channel_b

    def reset_eq(self, channel: str = "") -> None:
        """Reset EQ to flat. Empty string = focused channel."""
        ch = channel or self.active_channel
        target = self.channel_a if ch == "a" else self.channel_b
        target.eq_bass = 0
        target.eq_mid = 0
        target.eq_treble = 0
