"""Call record model."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CallRecord:
    direction: str          # 'inbound' or 'outbound'
    protocol: str           # 'SIP' or 'IAX'
    remote_number: str
    remote_name: str = ""
    status: str = "ringing"  # 'ringing', 'answered', 'missed', 'rejected', 'failed'
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    answered_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_seconds: int = 0
    db_id: Optional[int] = None

    def answer(self):
        self.status = "answered"
        self.answered_at = datetime.now().isoformat()

    def end(self):
        self.ended_at = datetime.now().isoformat()
        if self.answered_at:
            start = datetime.fromisoformat(self.answered_at)
            end = datetime.fromisoformat(self.ended_at)
            self.duration_seconds = int((end - start).total_seconds())
        if self.status == "ringing":
            self.status = "missed" if self.direction == "inbound" else "failed"

    @property
    def duration_display(self):
        mins, secs = divmod(self.duration_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours:d}:{mins:02d}:{secs:02d}"
        return f"{mins:d}:{secs:02d}"
