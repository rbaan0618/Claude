"""Base protocol handler interface."""

from abc import ABC, abstractmethod
from typing import Callable, Optional


class ProtocolHandler(ABC):
    """Abstract base class for VoIP protocol handlers."""

    def __init__(self):
        self.registered = False
        self.in_call = False
        self._on_incoming_call = None
        self._on_call_state_change = None
        self._on_registration_state = None
        self._on_blf_state_change = None
        self._on_message_received = None

    def set_callbacks(self,
                      on_incoming_call: Optional[Callable] = None,
                      on_call_state_change: Optional[Callable] = None,
                      on_registration_state: Optional[Callable] = None,
                      on_blf_state_change: Optional[Callable] = None,
                      on_message_received: Optional[Callable] = None):
        """Set event callbacks for the GUI to receive updates."""
        self._on_incoming_call = on_incoming_call
        self._on_call_state_change = on_call_state_change
        self._on_registration_state = on_registration_state
        self._on_blf_state_change = on_blf_state_change
        self._on_message_received = on_message_received

    @abstractmethod
    def initialize(self, config: dict) -> bool:
        """Initialize the protocol stack. Returns True on success."""
        pass

    @abstractmethod
    def register(self, server: str, username: str, password: str, port: int) -> bool:
        """Register with the server."""
        pass

    @abstractmethod
    def unregister(self):
        """Unregister from the server."""
        pass

    @abstractmethod
    def make_call(self, uri: str) -> bool:
        """Initiate an outbound call. Returns True if call started."""
        pass

    @abstractmethod
    def answer_call(self):
        """Answer an incoming call."""
        pass

    @abstractmethod
    def hangup_call(self):
        """Hang up the current call."""
        pass

    @abstractmethod
    def send_dtmf(self, digit: str):
        """Send a DTMF tone during an active call."""
        pass

    @abstractmethod
    def hold_call(self):
        """Place the current call on hold."""
        pass

    @abstractmethod
    def unhold_call(self):
        """Resume a held call."""
        pass

    @abstractmethod
    def transfer_call(self, target: str):
        """Blind transfer the current call to target."""
        pass

    @abstractmethod
    def subscribe_blf(self, extension: str):
        """Subscribe to BLF notifications for an extension."""
        pass

    @abstractmethod
    def shutdown(self):
        """Clean shutdown of the protocol stack."""
        pass

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        pass
