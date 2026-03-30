"""BLF (Busy Lamp Field) entry model."""

from dataclasses import dataclass


@dataclass
class BlfEntry:
    extension: str
    label: str = ""
    state: str = "unknown"  # 'idle', 'ringing', 'busy', 'unknown', 'offline'
    protocol: str = "SIP"

    @property
    def display_name(self):
        return self.label if self.label else self.extension

    @property
    def color(self):
        return {
            "idle": "#2ecc71",       # green
            "ringing": "#f39c12",    # orange
            "busy": "#e74c3c",       # red
            "unknown": "#95a5a6",    # gray
            "offline": "#7f8c8d",    # dark gray
        }.get(self.state, "#95a5a6")
