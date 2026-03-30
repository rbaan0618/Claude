"""IAX2 (Inter-Asterisk eXchange v2) protocol handler.

IAX2 is a VoIP protocol created by Digium for Asterisk. It multiplexes
signaling and media over a single UDP port (4569), making it firewall-friendly.

This handler implements IAX2 Mini and Full frames for:
  - Registration (REG REQ / REG ACK)
  - Call setup (NEW / ACCEPT / ANSWER)
  - Audio transport (voice frames)
  - DTMF
  - Hangup

Reference: RFC 5456 - IAX2 Protocol
"""

import socket
import struct
import hashlib
import threading
import time
import logging
import secrets
from typing import Optional
from protocols.base import ProtocolHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IAX2 frame constants (RFC 5456)
# ---------------------------------------------------------------------------
IAX2_PORT = 4569

# Frame types
FRAME_FULL = 0x8000  # Bit 16 set = full frame
AST_FRAME_VOICE = 2
AST_FRAME_IAX = 6
AST_FRAME_DTMF = 1
AST_FRAME_CONTROL = 4

# IAX subclass commands
IAX_CMD_NEW = 1
IAX_CMD_PING = 2
IAX_CMD_PONG = 3
IAX_CMD_ACK = 4
IAX_CMD_HANGUP = 5
IAX_CMD_REJECT = 6
IAX_CMD_ACCEPT = 7
IAX_CMD_AUTHREQ = 8
IAX_CMD_AUTHREP = 9
IAX_CMD_REGREQ = 13
IAX_CMD_REGAUTH = 14
IAX_CMD_REGACK = 15
IAX_CMD_REGREJ = 16
IAX_CMD_REGREL = 17
IAX_CMD_LAGRQ = 19
IAX_CMD_LAGRP = 20
IAX_CMD_ANSWER = 30

# Information Element types
IE_CALLED_NUMBER = 1
IE_CALLING_NUMBER = 2
IE_CALLING_NAME = 4
IE_USERNAME = 6
IE_PASSWORD = 7
IE_CAPABILITY = 8
IE_FORMAT = 9
IE_VERSION = 11
IE_AUTHMETHODS = 14
IE_CHALLENGE = 15
IE_MD5_RESULT = 16
IE_REFRESH = 19
IE_CAUSE = 22

# Audio codecs
CODEC_ULAW = 0x00000004
CODEC_ALAW = 0x00000008
CODEC_GSM = 0x00000002

# Control subtypes
CTRL_ANSWER = 5
CTRL_HANGUP = 1
CTRL_RINGING = 3
CTRL_BUSY = 7


class IaxHandler(ProtocolHandler):
    """IAX2 protocol handler with socket-level implementation."""

    def __init__(self):
        super().__init__()
        self._sock: Optional[socket.socket] = None
        self._server_addr = None
        self._username = ""
        self._password = ""
        self._source_call_number = 0
        self._dest_call_number = 0
        self._oseqno = 0
        self._iseqno = 0
        self._timestamp_base = 0
        self._recv_thread: Optional[threading.Thread] = None
        self._running = False
        self._config = {}
        self._call_state = "idle"
        self._reg_refresh = 60

    @property
    def protocol_name(self) -> str:
        return "IAX"

    # -- Frame construction helpers ------------------------------------------

    def _new_source_call_number(self):
        """Generate a random 15-bit source call number."""
        self._source_call_number = secrets.randbelow(0x7FFF) + 1
        return self._source_call_number

    def _timestamp(self):
        """Milliseconds since handler initialisation."""
        return int((time.time() - self._timestamp_base) * 1000) & 0xFFFFFFFF

    def _build_full_frame(self, frame_type, subclass, ies=b""):
        """Build an IAX2 full frame.

        Full frame format (12 bytes header):
          2 bytes: source call number (bit 15 set = full frame)
          2 bytes: destination call number
          4 bytes: timestamp
          1 byte:  OSeqno
          1 byte:  ISeqno
          1 byte:  frame type
          1 byte:  subclass
        """
        src = self._source_call_number | FRAME_FULL
        header = struct.pack("!HHIBBBB",
                             src,
                             self._dest_call_number,
                             self._timestamp(),
                             self._oseqno & 0xFF,
                             self._iseqno & 0xFF,
                             frame_type,
                             subclass)
        self._oseqno += 1
        return header + ies

    @staticmethod
    def _build_ie(ie_type, data):
        """Build a single Information Element: type(1) + length(1) + data."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif isinstance(data, int):
            if data <= 0xFF:
                data = struct.pack("!B", data)
            elif data <= 0xFFFF:
                data = struct.pack("!H", data)
            else:
                data = struct.pack("!I", data)
        return struct.pack("BB", ie_type, len(data)) + data

    def _parse_full_frame(self, data):
        """Parse an IAX2 full frame, return (header_dict, ies_dict)."""
        if len(data) < 12:
            return None, None
        src, dst, ts, oseq, iseq, ftype, subclass = struct.unpack("!HHIBBBB", data[:12])
        is_full = bool(src & FRAME_FULL)
        header = {
            "source": src & 0x7FFF,
            "dest": dst,
            "timestamp": ts,
            "oseqno": oseq,
            "iseqno": iseq,
            "type": ftype,
            "subclass": subclass,
            "full": is_full,
        }
        # Parse IEs
        ies = {}
        pos = 12
        while pos + 2 <= len(data):
            ie_type = data[pos]
            ie_len = data[pos + 1]
            ie_data = data[pos + 2:pos + 2 + ie_len]
            ies[ie_type] = ie_data
            pos += 2 + ie_len
        return header, ies

    def _send(self, frame_data):
        """Send raw frame data to the server."""
        if self._sock and self._server_addr:
            try:
                self._sock.sendto(frame_data, self._server_addr)
            except OSError as e:
                logger.error("IAX send error: %s", e)

    def _send_ack(self, header):
        """Send an ACK for a received full frame."""
        self._iseqno = (header["oseqno"] + 1) & 0xFF
        ack = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_ACK)
        self._send(ack)

    # -- Protocol operations ------------------------------------------------

    def initialize(self, config: dict) -> bool:
        self._config = config
        self._timestamp_base = time.time()
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(1.0)
            local_port = int(config.get("local_port", 0)) or 0
            self._sock.bind(("", local_port))
            logger.info("IAX2 bound to local port %s", local_port or "auto")
            self._running = True
            self._recv_thread = threading.Thread(target=self._receive_loop,
                                                 daemon=True, name="iax-recv")
            self._recv_thread.start()
            logger.info("IAX2 handler initialized")
            return True
        except Exception as e:
            logger.error("IAX2 init failed: %s", e)
            return False

    def register(self, server: str, username: str, password: str, port: int = 4569) -> bool:
        self._server_addr = (server, port)
        self._username = username
        self._password = password
        self._new_source_call_number()
        self._oseqno = 0
        self._iseqno = 0

        ies = (self._build_ie(IE_USERNAME, username) +
               self._build_ie(IE_REFRESH, self._reg_refresh))
        frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_REGREQ, ies)
        self._send(frame)
        logger.info("IAX2 REG REQ sent to %s:%d as %s", server, port, username)
        return True

    def unregister(self):
        if not self._server_addr:
            return
        ies = self._build_ie(IE_USERNAME, self._username)
        frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_REGREL, ies)
        self._send(frame)
        self.registered = False
        if self._on_registration_state:
            self._on_registration_state("IAX", False, 0)

    def make_call(self, uri: str) -> bool:
        if self.in_call:
            logger.warning("IAX: already in a call")
            return False
        if not self._server_addr:
            logger.error("IAX: not registered")
            return False

        self._new_source_call_number()
        self._oseqno = 0
        self._iseqno = 0

        ies = (self._build_ie(IE_CALLED_NUMBER, uri) +
               self._build_ie(IE_CALLING_NUMBER, self._username) +
               self._build_ie(IE_CALLING_NAME, self._config.get("display_name", self._username)) +
               self._build_ie(IE_USERNAME, self._username) +
               self._build_ie(IE_FORMAT, CODEC_ULAW) +
               self._build_ie(IE_CAPABILITY, CODEC_ULAW | CODEC_ALAW) +
               self._build_ie(IE_VERSION, 2))
        frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_NEW, ies)
        self._send(frame)
        self.in_call = True
        self._call_state = "calling"
        if self._on_call_state_change:
            self._on_call_state_change("IAX", "CALLING", "")
        return True

    def answer_call(self):
        frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_ANSWER)
        self._send(frame)
        self._call_state = "answered"
        if self._on_call_state_change:
            self._on_call_state_change("IAX", "CONFIRMED", "")

    def hangup_call(self):
        ies = self._build_ie(IE_CAUSE, "Normal Clearing")
        frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_HANGUP, ies)
        self._send(frame)
        self.in_call = False
        self._call_state = "idle"
        self._dest_call_number = 0
        if self._on_call_state_change:
            self._on_call_state_change("IAX", "DISCONNECTED", "Normal")

    def send_dtmf(self, digit: str):
        if not self.in_call:
            return
        # DTMF is sent as AST_FRAME_DTMF with subclass = ASCII of digit
        frame = self._build_full_frame(AST_FRAME_DTMF, ord(digit))
        self._send(frame)

    def hold_call(self):
        # IAX2 doesn't have native hold; use music-on-hold or stop sending audio
        logger.info("IAX hold (stop audio tx)")
        if self._on_call_state_change:
            self._on_call_state_change("IAX", "HOLD", "")

    def unhold_call(self):
        logger.info("IAX unhold (resume audio tx)")
        if self._on_call_state_change:
            self._on_call_state_change("IAX", "CONFIRMED", "")

    def transfer_call(self, target: str):
        # IAX2 native transfer via TRANSFER command
        ies = self._build_ie(IE_CALLED_NUMBER, target)
        frame = self._build_full_frame(AST_FRAME_IAX, 24, ies)  # 24 = TRANSFER
        self._send(frame)
        logger.info("IAX transfer to %s", target)

    def subscribe_blf(self, extension: str):
        # IAX2 doesn't natively support BLF/presence; would need Asterisk manager
        logger.info("IAX BLF not natively supported for %s", extension)

    def shutdown(self):
        self._running = False
        if self.in_call:
            self.hangup_call()
        if self.registered:
            self.unregister()
        if self._sock:
            self._sock.close()
        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        logger.info("IAX2 handler shut down")

    # -- Receive loop -------------------------------------------------------

    def _receive_loop(self):
        """Background thread that receives and processes IAX2 frames."""
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < 4:
                continue

            # Check if full frame (bit 15 of first 2 bytes)
            src_raw = struct.unpack("!H", data[:2])[0]
            if src_raw & FRAME_FULL:
                self._handle_full_frame(data, addr)
            else:
                self._handle_mini_frame(data, addr)

    def _handle_full_frame(self, data, addr):
        header, ies = self._parse_full_frame(data)
        if header is None:
            return

        ftype = header["type"]
        subclass = header["subclass"]

        # Always update dest call number
        if header["source"] and self._dest_call_number == 0:
            self._dest_call_number = header["source"]

        if ftype == AST_FRAME_IAX:
            self._handle_iax_command(header, ies)
        elif ftype == AST_FRAME_CONTROL:
            self._handle_control(header, subclass)
        elif ftype == AST_FRAME_VOICE:
            pass  # Audio data — would feed to audio output
        elif ftype == AST_FRAME_DTMF:
            logger.info("IAX DTMF received: %s", chr(subclass))

    def _handle_iax_command(self, header, ies):
        subclass = header["subclass"]
        self._send_ack(header)

        if subclass == IAX_CMD_REGAUTH:
            # Server wants authentication — respond with MD5
            challenge = ies.get(IE_CHALLENGE, b"").decode("utf-8", errors="replace")
            md5_input = challenge + self._password
            md5_result = hashlib.md5(md5_input.encode("utf-8")).hexdigest()
            resp_ies = (self._build_ie(IE_USERNAME, self._username) +
                        self._build_ie(IE_MD5_RESULT, md5_result) +
                        self._build_ie(IE_REFRESH, self._reg_refresh))
            frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_REGREQ, resp_ies)
            self._send(frame)

        elif subclass == IAX_CMD_REGACK:
            self.registered = True
            logger.info("IAX2 registered successfully")
            if self._on_registration_state:
                self._on_registration_state("IAX", True, 200)
            # Schedule re-registration
            threading.Timer(self._reg_refresh - 5, self._re_register).start()

        elif subclass == IAX_CMD_REGREJ:
            self.registered = False
            cause = ies.get(IE_CAUSE, b"Rejected").decode("utf-8", errors="replace")
            logger.warning("IAX2 registration rejected: %s", cause)
            if self._on_registration_state:
                self._on_registration_state("IAX", False, 401)

        elif subclass == IAX_CMD_AUTHREQ:
            # Call authentication
            challenge = ies.get(IE_CHALLENGE, b"").decode("utf-8", errors="replace")
            md5_input = challenge + self._password
            md5_result = hashlib.md5(md5_input.encode("utf-8")).hexdigest()
            resp_ies = self._build_ie(IE_MD5_RESULT, md5_result)
            frame = self._build_full_frame(AST_FRAME_IAX, IAX_CMD_AUTHREP, resp_ies)
            self._send(frame)

        elif subclass == IAX_CMD_ACCEPT:
            logger.info("IAX2 call accepted")
            self._call_state = "accepted"

        elif subclass == IAX_CMD_ANSWER:
            self._call_state = "answered"
            self.in_call = True
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "CONFIRMED", "")

        elif subclass == IAX_CMD_HANGUP:
            cause = ies.get(IE_CAUSE, b"").decode("utf-8", errors="replace")
            self.in_call = False
            self._call_state = "idle"
            self._dest_call_number = 0
            logger.info("IAX2 remote hangup: %s", cause)
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "DISCONNECTED", cause)

        elif subclass == IAX_CMD_REJECT:
            cause = ies.get(IE_CAUSE, b"").decode("utf-8", errors="replace")
            self.in_call = False
            self._call_state = "idle"
            logger.info("IAX2 call rejected: %s", cause)
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "REJECTED", cause)

        elif subclass == IAX_CMD_NEW:
            # Incoming call
            self._dest_call_number = header["source"]
            called = ies.get(IE_CALLED_NUMBER, b"").decode("utf-8", errors="replace")
            caller_name = ies.get(IE_CALLING_NAME, b"").decode("utf-8", errors="replace")
            caller_num = ies.get(IE_CALLING_NUMBER, b"").decode("utf-8", errors="replace")
            self.in_call = True
            self._call_state = "ringing"
            logger.info("IAX2 incoming call from %s <%s>", caller_name, caller_num)
            if self._on_incoming_call:
                self._on_incoming_call("IAX", caller_num, caller_name)

        elif subclass in (IAX_CMD_PING, IAX_CMD_LAGRQ):
            # Respond with PONG / LAGRP
            resp_cmd = IAX_CMD_PONG if subclass == IAX_CMD_PING else IAX_CMD_LAGRP
            frame = self._build_full_frame(AST_FRAME_IAX, resp_cmd)
            self._send(frame)

        elif subclass == IAX_CMD_ACK:
            pass  # Acknowledgement — nothing to do

    def _handle_control(self, header, subclass):
        self._send_ack(header)
        if subclass == CTRL_RINGING:
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "RINGING", "")
        elif subclass == CTRL_ANSWER:
            self._call_state = "answered"
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "CONFIRMED", "")
        elif subclass == CTRL_BUSY:
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "BUSY", "")
        elif subclass == CTRL_HANGUP:
            self.in_call = False
            self._call_state = "idle"
            if self._on_call_state_change:
                self._on_call_state_change("IAX", "DISCONNECTED", "Remote Hangup")

    def _handle_mini_frame(self, data, addr):
        """Mini frames carry audio — 4 byte header + audio payload."""
        # Would feed audio to playback device
        pass

    def _re_register(self):
        """Periodic re-registration."""
        if self._running and self._server_addr:
            self.register(self._server_addr[0], self._username,
                          self._password, self._server_addr[1])
