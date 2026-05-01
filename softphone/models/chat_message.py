"""Chat message data model (SIP MESSAGE / RFC 3428)."""

from dataclasses import dataclass


@dataclass
class ChatMessage:
    peer: str
    direction: str   # 'in' or 'out'
    body: str
    timestamp: float
    read: int = 0
