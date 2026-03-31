"""SIP protocol handler using native Python sockets.

Implements SIP signaling (RFC 3261) over UDP with:
  - REGISTER with digest authentication (RFC 2617)
  - INVITE / ACK / BYE for call setup and teardown
  - rport support (RFC 3581) for NAT traversal
  - RTP audio using pyaudio
  - DTMF via RTP (RFC 2833 / RFC 4733)
  - BLF via SUBSCRIBE/NOTIFY (RFC 3265 + dialog event package)
"""

import socket
import threading
import hashlib
import time
import random
import struct
import logging
import re
from typing import Optional
from protocols.base import ProtocolHandler

logger = logging.getLogger(__name__)

# Try to import pyaudio for RTP audio
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    logger.warning("pyaudio not installed — no audio. Install with: pip install pyaudio")


def _generate_tag():
    return f"{random.randint(100000, 999999)}"


def _generate_branch():
    return f"z9hG4bK{random.randint(100000000, 999999999)}"


def _generate_call_id():
    return f"{random.randint(10000000, 99999999)}@pysoftphone"


class RtpSession:
    """Handles RTP audio send/receive using pyaudio."""

    PCMU_PAYLOAD_TYPE = 0   # G.711 u-law
    DTMF_PAYLOAD_TYPE = 101  # RFC 2833 telephone-event
    SAMPLE_RATE = 8000
    FRAME_SIZE = 160  # 20ms at 8000Hz
    PTIME = 20  # ms

    def __init__(self, local_port):
        self._local_port = local_port
        self._remote_addr = None
        self._sock = None
        self._running = False
        self._seq = random.randint(0, 65535)
        self._timestamp = random.randint(0, 0xFFFFFFFF)
        self._ssrc = random.randint(0, 0xFFFFFFFF)
        self._recv_thread = None
        self._send_thread = None
        self._pa = None
        self._input_stream = None
        self._output_stream = None
        self._muted = False

    def start(self, remote_ip, remote_port):
        """Start RTP audio session."""
        self._remote_addr = (remote_ip, remote_port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)
        self._sock.bind(("", self._local_port))
        actual_port = self._sock.getsockname()[1]
        self._local_port = actual_port
        self._running = True

        if PYAUDIO_AVAILABLE:
            try:
                self._pa = pyaudio.PyAudio()
                self._input_stream = self._pa.open(
                    format=pyaudio.paInt16, channels=1,
                    rate=self.SAMPLE_RATE, input=True,
                    frames_per_buffer=self.FRAME_SIZE)
                self._output_stream = self._pa.open(
                    format=pyaudio.paInt16, channels=1,
                    rate=self.SAMPLE_RATE, output=True,
                    frames_per_buffer=self.FRAME_SIZE)
                logger.info("RTP audio streams opened (port %d -> %s:%d)",
                            actual_port, remote_ip, remote_port)
            except Exception as e:
                logger.error("Failed to open audio: %s", e)
                self._pa = None

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True, name="rtp-recv")
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True, name="rtp-send")
        self._recv_thread.start()
        self._send_thread.start()
        return actual_port

    def stop(self):
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        if self._send_thread:
            self._send_thread.join(timeout=2)
        if self._input_stream:
            self._input_stream.stop_stream()
            self._input_stream.close()
        if self._output_stream:
            self._output_stream.stop_stream()
            self._output_stream.close()
        if self._pa:
            self._pa.terminate()
        if self._sock:
            self._sock.close()
        logger.info("RTP session stopped")

    @property
    def local_port(self):
        return self._local_port

    def set_muted(self, muted):
        self._muted = muted

    def _linear_to_ulaw(self, sample):
        """Convert 16-bit linear PCM to u-law."""
        BIAS = 0x84
        MAX = 0x7FFF
        sign = 0
        if sample < 0:
            sign = 0x80
            sample = -sample
        if sample > MAX:
            sample = MAX
        sample += BIAS
        exponent = 7
        for exp_mask in [0x4000, 0x2000, 0x1000, 0x0800, 0x0400, 0x0200, 0x0100]:
            if sample & exp_mask:
                break
            exponent -= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        return ~(sign | (exponent << 4) | mantissa) & 0xFF

    def _ulaw_to_linear(self, ulawbyte):
        """Convert u-law to 16-bit linear PCM."""
        ulawbyte = ~ulawbyte & 0xFF
        sign = ulawbyte & 0x80
        exponent = (ulawbyte >> 4) & 0x07
        mantissa = ulawbyte & 0x0F
        sample = (mantissa << (exponent + 3)) + (0x84 << exponent) - 0x84
        if sign:
            sample = -sample
        return sample

    def _send_loop(self):
        """Capture mic audio and send as RTP."""
        while self._running:
            if not self._input_stream or self._muted:
                time.sleep(self.PTIME / 1000.0)
                continue
            try:
                pcm_data = self._input_stream.read(self.FRAME_SIZE, exception_on_overflow=False)
                # Convert 16-bit PCM to u-law
                samples = struct.unpack(f"<{self.FRAME_SIZE}h", pcm_data)
                ulaw_payload = bytes(self._linear_to_ulaw(s) for s in samples)

                # Build RTP header: V=2, P=0, X=0, CC=0, M=0, PT=0
                rtp_header = struct.pack("!BBHII",
                                         0x80,  # V=2
                                         self.PCMU_PAYLOAD_TYPE,
                                         self._seq & 0xFFFF,
                                         self._timestamp & 0xFFFFFFFF,
                                         self._ssrc)
                self._sock.sendto(rtp_header + ulaw_payload, self._remote_addr)
                self._seq += 1
                self._timestamp += self.FRAME_SIZE
            except Exception:
                if self._running:
                    time.sleep(self.PTIME / 1000.0)

    def _recv_loop(self):
        """Receive RTP packets and play through speaker."""
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < 12:
                continue

            # Parse RTP header
            pt = data[1] & 0x7F
            if pt == self.PCMU_PAYLOAD_TYPE and self._output_stream:
                payload = data[12:]
                # Convert u-law to 16-bit PCM
                samples = [self._ulaw_to_linear(b) for b in payload]
                pcm_data = struct.pack(f"<{len(samples)}h", *samples)
                try:
                    self._output_stream.write(pcm_data)
                except Exception:
                    pass

    def send_dtmf(self, digit):
        """Send DTMF via RFC 2833 RTP events."""
        dtmf_map = {
            '0': 0, '1': 1, '2': 2, '3': 3, '4': 4,
            '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
            '*': 10, '#': 11,
        }
        event = dtmf_map.get(digit, 0)
        # Send start events (3 packets)
        for i in range(3):
            rtp_header = struct.pack("!BBHII",
                                     0x80,
                                     self.DTMF_PAYLOAD_TYPE | (0x80 if i == 0 else 0),
                                     self._seq & 0xFFFF,
                                     self._timestamp & 0xFFFFFFFF,
                                     self._ssrc)
            # RFC 2833 payload: event(1), E+R+volume(1), duration(2)
            payload = struct.pack("!BBH", event, 10, 160 * (i + 1))
            self._sock.sendto(rtp_header + payload, self._remote_addr)
            self._seq += 1
            time.sleep(0.02)

        # Send end events (3 packets)
        for i in range(3):
            rtp_header = struct.pack("!BBHII",
                                     0x80,
                                     self.DTMF_PAYLOAD_TYPE,
                                     self._seq & 0xFFFF,
                                     self._timestamp & 0xFFFFFFFF,
                                     self._ssrc)
            payload = struct.pack("!BBH", event, 0x80 | 10, 160 * 8)  # E=1 (end)
            self._sock.sendto(rtp_header + payload, self._remote_addr)
            self._seq += 1
        self._timestamp += self.FRAME_SIZE * 8


class SipHandler(ProtocolHandler):
    """SIP protocol handler using native UDP sockets."""

    def __init__(self):
        super().__init__()
        self._sock: Optional[socket.socket] = None
        self._config = {}
        self._server_addr = None
        self._local_ip = ""
        self._local_port = 5060
        self._username = ""
        self._password = ""
        self._display_name = ""
        self._use_rport = True
        self._recv_thread: Optional[threading.Thread] = None
        self._running = False
        # Registration state
        self._reg_call_id = ""
        self._reg_cseq = 0
        self._reg_from_tag = ""
        self._reg_expires = 120
        self._reg_timer: Optional[threading.Timer] = None
        # Call state
        self._call_id = ""
        self._call_cseq = 0
        self._call_from_tag = ""
        self._call_to_tag = ""
        self._call_remote_uri = ""
        self._call_remote_target = ""
        self._call_route_set = []
        self._rtp_session: Optional[RtpSession] = None
        self._rtp_port = 0
        # BLF
        self._blf_subscriptions = {}

    @property
    def protocol_name(self) -> str:
        return "SIP"

    def _get_local_ip(self, server):
        """Determine the local IP that can reach the server."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((server, 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"

    def _via_header(self, branch=None):
        branch = branch or _generate_branch()
        via = f"SIP/2.0/UDP {self._local_ip}:{self._local_port};branch={branch}"
        if self._use_rport:
            via += ";rport"
        return via

    def _contact_header(self):
        return f"<sip:{self._username}@{self._local_ip}:{self._local_port}>"

    def _send_sip(self, message):
        """Send a SIP message to the server."""
        if self._sock and self._server_addr:
            data = message.encode("utf-8")
            try:
                self._sock.sendto(data, self._server_addr)
                logger.debug("SIP TX:\n%s", message[:200])
            except OSError as e:
                logger.error("SIP send error: %s", e)

    def _parse_response(self, data):
        """Parse a SIP response into status code, headers dict, and body."""
        text = data.decode("utf-8", errors="replace")
        parts = text.split("\r\n\r\n", 1)
        header_section = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        lines = header_section.split("\r\n")
        status_line = lines[0]

        # Parse status code
        match = re.match(r"SIP/2\.0\s+(\d+)", status_line)
        status_code = int(match.group(1)) if match else 0

        # Check if it's a request instead (INVITE, BYE, etc)
        req_match = re.match(r"^(\w+)\s+(\S+)\s+SIP/2\.0", status_line)
        method = req_match.group(1) if req_match else None

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key in headers:
                    if isinstance(headers[key], list):
                        headers[key].append(value)
                    else:
                        headers[key] = [headers[key], value]
                else:
                    headers[key] = value

        return status_code, method, headers, body

    def _extract_auth_params(self, auth_header):
        """Extract realm, nonce, etc from WWW-Authenticate or Proxy-Authenticate."""
        params = {}
        for match in re.finditer(r'(\w+)="([^"]*)"', auth_header):
            params[match.group(1)] = match.group(2)
        for match in re.finditer(r'(\w+)=([^",\s]+)', auth_header):
            if match.group(1) not in params:
                params[match.group(1)] = match.group(2)
        return params

    def _make_digest_response(self, method, uri, realm, nonce, username, password):
        """Compute SIP digest authentication response (RFC 2617)."""
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return response

    def _build_auth_header(self, method, uri, auth_header, header_name="Authorization"):
        """Build a Digest authorization header."""
        params = self._extract_auth_params(auth_header)
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        response = self._make_digest_response(method, uri, realm, nonce,
                                               self._username, self._password)
        return (f'{header_name}: Digest username="{self._username}", '
                f'realm="{realm}", nonce="{nonce}", uri="{uri}", '
                f'response="{response}", algorithm=MD5')

    def _parse_sdp(self, body):
        """Extract RTP IP and port from SDP body."""
        ip = ""
        port = 0
        for line in body.split("\r\n"):
            if line.startswith("c=IN IP4 "):
                ip = line.split()[-1]
            elif line.startswith("m=audio "):
                parts = line.split()
                port = int(parts[1])
        return ip, port

    def _build_sdp(self):
        """Build SDP offer with audio on our RTP port."""
        # Allocate RTP port
        rtp_port = self._rtp_port or (self._local_port + 2)
        sdp = (
            "v=0\r\n"
            f"o=pysoftphone 0 0 IN IP4 {self._local_ip}\r\n"
            "s=PySoftphone\r\n"
            f"c=IN IP4 {self._local_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        return sdp, rtp_port

    # -- Protocol interface --------------------------------------------------

    def initialize(self, config: dict) -> bool:
        self._config = config
        self._use_rport = config.get("rport", True)
        self._local_port = int(config.get("local_port", 5060)) or 5060
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(1.0)
            self._sock.bind(("", self._local_port))
            actual = self._sock.getsockname()[1]
            self._local_port = actual
            self._running = True
            self._recv_thread = threading.Thread(target=self._recv_loop,
                                                 daemon=True, name="sip-recv")
            self._recv_thread.start()
            logger.info("SIP handler initialized on local port %d (rport=%s)",
                        self._local_port, self._use_rport)
            return True
        except Exception as e:
            logger.error("SIP init failed: %s", e)
            return False

    def register(self, server: str, username: str, password: str, port: int = 5060) -> bool:
        self._server_addr = (server, port)
        self._username = username
        self._password = password
        self._display_name = self._config.get("display_name", username)
        self._local_ip = self._get_local_ip(server)
        self._reg_call_id = _generate_call_id()
        self._reg_from_tag = _generate_tag()
        self._reg_cseq += 1

        from_uri = f'"{self._display_name}" <sip:{username}@{server}>'
        to_uri = f"<sip:{username}@{server}>"
        request_uri = f"sip:{server}:{port}"

        msg = (
            f"REGISTER {request_uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._reg_from_tag}\r\n"
            f"To: {to_uri}\r\n"
            f"Call-ID: {self._reg_call_id}\r\n"
            f"CSeq: {self._reg_cseq} REGISTER\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Expires: {self._reg_expires}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REGISTER sent to %s:%d as %s", server, port, username)
        return True

    def _send_register_with_auth(self, auth_header):
        """Re-send REGISTER with digest authentication."""
        server, port = self._server_addr
        self._reg_cseq += 1
        request_uri = f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_uri = f"<sip:{self._username}@{server}>"

        auth_line = self._build_auth_header("REGISTER", request_uri, auth_header)

        msg = (
            f"REGISTER {request_uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._reg_from_tag}\r\n"
            f"To: {to_uri}\r\n"
            f"Call-ID: {self._reg_call_id}\r\n"
            f"CSeq: {self._reg_cseq} REGISTER\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Expires: {self._reg_expires}\r\n"
            f"{auth_line}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REGISTER with auth sent")

    def unregister(self):
        if not self._server_addr:
            return
        server, port = self._server_addr
        self._reg_cseq += 1
        request_uri = f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_uri = f"<sip:{self._username}@{server}>"

        msg = (
            f"REGISTER {request_uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._reg_from_tag}\r\n"
            f"To: {to_uri}\r\n"
            f"Call-ID: {self._reg_call_id}\r\n"
            f"CSeq: {self._reg_cseq} REGISTER\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Expires: 0\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        self.registered = False
        if self._reg_timer:
            self._reg_timer.cancel()
        if self._on_registration_state:
            self._on_registration_state("SIP", False, 0)

    def make_call(self, uri: str) -> bool:
        if self.in_call:
            logger.warning("Already in a call")
            return False
        if not self._server_addr:
            logger.error("Not registered")
            return False

        server, port = self._server_addr
        # Normalize URI
        if not uri.startswith("sip:"):
            uri = f"sip:{uri}@{server}"
        self._call_remote_uri = uri

        self._call_id = _generate_call_id()
        self._call_from_tag = _generate_tag()
        self._call_to_tag = ""
        self._call_cseq = 1

        sdp_body, rtp_port = self._build_sdp()
        self._rtp_port = rtp_port
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'

        msg = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        self._send_sip(msg)
        self.in_call = True
        logger.info("SIP INVITE sent to %s", uri)
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "CALLING", "")
        return True

    def _send_invite_with_auth(self, auth_header):
        """Re-send INVITE with digest auth."""
        server, port = self._server_addr
        self._call_cseq += 1
        sdp_body, rtp_port = self._build_sdp()
        self._rtp_port = rtp_port
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        uri = self._call_remote_uri

        auth_line = self._build_auth_header("INVITE", uri, auth_header)

        msg = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"{auth_line}\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        self._send_sip(msg)
        logger.info("SIP INVITE with auth sent")

    def _send_ack(self, to_tag=""):
        """Send ACK for an INVITE transaction."""
        server, port = self._server_addr
        uri = self._call_remote_uri or f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_header = f"<{uri}>"
        if to_tag:
            to_header += f";tag={to_tag}"

        msg = (
            f"ACK {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} ACK\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)

    def answer_call(self):
        """Send 200 OK to an incoming INVITE."""
        if not self._call_id:
            return
        server, port = self._server_addr
        sdp_body, rtp_port = self._build_sdp()
        self._rtp_port = rtp_port
        to_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'

        msg = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {self._incoming_via}\r\n"
            f"From: {self._incoming_from}\r\n"
            f"To: {to_uri};tag={self._call_from_tag}\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._incoming_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        self._send_sip(msg)

        # Start RTP
        if self._incoming_rtp_ip and self._incoming_rtp_port:
            self._start_rtp(self._incoming_rtp_ip, self._incoming_rtp_port)

        if self._on_call_state_change:
            self._on_call_state_change("SIP", "CONFIRMED", "")

    def hangup_call(self):
        if not self._call_id or not self._server_addr:
            self.in_call = False
            return
        server, port = self._server_addr
        self._call_cseq += 1
        uri = self._call_remote_uri or f"sip:{server}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_header = f"<{uri}>"
        if self._call_to_tag:
            to_header += f";tag={self._call_to_tag}"

        msg = (
            f"BYE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} BYE\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        self._stop_rtp()
        self.in_call = False
        self._call_id = ""
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "DISCONNECTED", "Normal")

    def send_dtmf(self, digit: str):
        if self._rtp_session:
            self._rtp_session.send_dtmf(digit)

    def hold_call(self):
        if self._rtp_session:
            self._rtp_session.set_muted(True)
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "HOLD", "")

    def unhold_call(self):
        if self._rtp_session:
            self._rtp_session.set_muted(False)
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "CONFIRMED", "")

    def transfer_call(self, target: str):
        """Blind transfer via SIP REFER."""
        if not self._call_id or not self._server_addr:
            return
        server, port = self._server_addr
        if not target.startswith("sip:"):
            target = f"sip:{target}@{server}"
        self._call_cseq += 1
        uri = self._call_remote_uri or f"sip:{server}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_header = f"<{uri}>"
        if self._call_to_tag:
            to_header += f";tag={self._call_to_tag}"

        msg = (
            f"REFER {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} REFER\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Refer-To: <{target}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REFER sent to transfer to %s", target)

    def subscribe_blf(self, extension: str):
        """Subscribe to dialog event package for BLF."""
        if not self._server_addr:
            return
        server, port = self._server_addr
        sub_call_id = _generate_call_id()
        sub_tag = _generate_tag()
        self._blf_subscriptions[extension] = {
            "call_id": sub_call_id, "tag": sub_tag, "cseq": 1
        }

        uri = f"sip:{extension}@{server}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'

        msg = (
            f"SUBSCRIBE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={sub_tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {sub_call_id}\r\n"
            f"CSeq: 1 SUBSCRIBE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: PySoftphone/1.0\r\n"
            f"Event: dialog\r\n"
            f"Accept: application/dialog-info+xml\r\n"
            f"Expires: 3600\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP SUBSCRIBE (BLF) sent for %s", extension)

    def shutdown(self):
        self._running = False
        if self._reg_timer:
            self._reg_timer.cancel()
        if self.in_call:
            self.hangup_call()
        if self.registered:
            self.unregister()
        self._stop_rtp()
        if self._sock:
            self._sock.close()
        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        self.registered = False
        self.in_call = False
        logger.info("SIP handler shut down")

    # -- RTP management ------------------------------------------------------

    def _start_rtp(self, remote_ip, remote_port):
        """Start RTP audio session."""
        self._stop_rtp()
        self._rtp_session = RtpSession(self._rtp_port)
        actual_port = self._rtp_session.start(remote_ip, remote_port)
        self._rtp_port = actual_port
        logger.info("RTP session started: local=%d remote=%s:%d",
                     actual_port, remote_ip, remote_port)

    def _stop_rtp(self):
        if self._rtp_session:
            self._rtp_session.stop()
            self._rtp_session = None

    # -- Receive loop --------------------------------------------------------

    def _recv_loop(self):
        """Background thread receiving SIP messages."""
        while self._running:
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                self._handle_message(data, addr)
            except Exception as e:
                logger.error("Error handling SIP message: %s", e, exc_info=True)

    def _handle_message(self, data, addr):
        status_code, method, headers, body = self._parse_response(data)
        call_id = headers.get("call-id", "")

        # --- Incoming request (INVITE, BYE, NOTIFY, etc) ---
        if method:
            self._handle_request(method, headers, body, addr)
            return

        # --- Responses to our transactions ---
        cseq = headers.get("cseq", "")

        # Registration responses
        if "REGISTER" in cseq:
            self._handle_register_response(status_code, headers, body)
        elif "INVITE" in cseq:
            self._handle_invite_response(status_code, headers, body)
        elif "BYE" in cseq:
            logger.info("BYE response: %d", status_code)
        elif "SUBSCRIBE" in cseq:
            logger.info("SUBSCRIBE response: %d", status_code)

    def _handle_register_response(self, status_code, headers, body):
        if status_code == 200:
            self.registered = True
            logger.info("SIP registered successfully")
            if self._on_registration_state:
                self._on_registration_state("SIP", True, 200)
            # Schedule re-registration
            if self._reg_timer:
                self._reg_timer.cancel()
            self._reg_timer = threading.Timer(
                self._reg_expires - 30, self._re_register)
            self._reg_timer.daemon = True
            self._reg_timer.start()

        elif status_code == 401 or status_code == 407:
            # Authentication required
            auth_header = headers.get("www-authenticate", "")
            if not auth_header:
                auth_header = headers.get("proxy-authenticate", "")
            if auth_header:
                if isinstance(auth_header, list):
                    auth_header = auth_header[0]
                self._send_register_with_auth(auth_header)
            else:
                logger.error("SIP 401 but no auth header")
                if self._on_registration_state:
                    self._on_registration_state("SIP", False, status_code)

        elif status_code == 403:
            logger.error("SIP registration forbidden (403)")
            if self._on_registration_state:
                self._on_registration_state("SIP", False, 403)

        else:
            logger.warning("SIP REGISTER response: %d", status_code)
            if status_code >= 400:
                if self._on_registration_state:
                    self._on_registration_state("SIP", False, status_code)

    def _handle_invite_response(self, status_code, headers, body):
        if status_code == 100:
            logger.info("SIP 100 Trying")
        elif status_code == 180 or status_code == 183:
            logger.info("SIP %d Ringing", status_code)
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "RINGING", "")
        elif status_code == 200:
            # Call answered — extract To tag and start RTP
            to_header = headers.get("to", "")
            tag_match = re.search(r'tag=([^;>\s]+)', to_header)
            if tag_match:
                self._call_to_tag = tag_match.group(1)

            # Parse SDP for remote RTP address
            if body:
                rtp_ip, rtp_port = self._parse_sdp(body)
                if rtp_ip and rtp_port:
                    self._start_rtp(rtp_ip, rtp_port)

            # Send ACK
            self._send_ack(self._call_to_tag)
            logger.info("SIP call connected")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "CONFIRMED", "")

        elif status_code == 401 or status_code == 407:
            # Need auth for INVITE
            auth_header = headers.get("www-authenticate", "")
            if not auth_header:
                auth_header = headers.get("proxy-authenticate", "")
            if auth_header:
                if isinstance(auth_header, list):
                    auth_header = auth_header[0]
                # ACK the 401
                self._send_ack()
                self._send_invite_with_auth(auth_header)

        elif status_code == 486 or status_code == 600:
            self._send_ack()
            self.in_call = False
            self._call_id = ""
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "BUSY", "Busy")

        elif status_code >= 400:
            self._send_ack()
            self.in_call = False
            self._call_id = ""
            logger.warning("SIP INVITE rejected: %d", status_code)
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "REJECTED", str(status_code))

    def _handle_request(self, method, headers, body, addr):
        """Handle incoming SIP requests."""
        call_id = headers.get("call-id", "")
        from_header = headers.get("from", "")
        to_header = headers.get("to", "")
        via_header = headers.get("via", "")
        if isinstance(via_header, list):
            via_header = via_header[0]
        cseq = headers.get("cseq", "")

        if method == "INVITE":
            self._handle_incoming_invite(headers, body, addr)

        elif method == "BYE":
            # Remote hangup
            # Send 200 OK
            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send_sip(response)
            self._stop_rtp()
            self.in_call = False
            self._call_id = ""
            logger.info("SIP remote BYE received")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "DISCONNECTED", "Remote hangup")

        elif method == "NOTIFY":
            # BLF notification — send 200 OK
            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send_sip(response)
            # Parse dialog-info for BLF state
            self._parse_blf_notify(body, from_header)

        elif method == "OPTIONS":
            # Keepalive / NAT check — respond 200 OK
            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                f"User-Agent: PySoftphone/1.0\r\n"
                f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY, REFER\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send_sip(response)

        elif method == "ACK":
            pass  # ACK for our 200 OK to incoming INVITE

        elif method == "CANCEL":
            # Respond 200 OK to CANCEL, then 487 to original INVITE
            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send_sip(response)
            self.in_call = False
            self._call_id = ""
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "DISCONNECTED", "Cancelled")

    def _handle_incoming_invite(self, headers, body, addr):
        """Handle incoming INVITE."""
        call_id = headers.get("call-id", "")
        from_header = headers.get("from", "")
        via_header = headers.get("via", "")
        if isinstance(via_header, list):
            via_header = "\r\nVia: ".join(via_header)
        cseq_header = headers.get("cseq", "")

        # Store for answer
        self._call_id = call_id
        self._incoming_via = via_header
        self._incoming_from = from_header
        self._incoming_cseq = re.match(r"(\d+)", cseq_header).group(1) if cseq_header else "1"
        self._call_from_tag = _generate_tag()

        # Extract caller info from From header
        name_match = re.match(r'"([^"]*)"', from_header)
        uri_match = re.search(r'sip:([^@>]+)@', from_header)
        caller_name = name_match.group(1) if name_match else ""
        caller_num = uri_match.group(1) if uri_match else from_header

        # Extract remote URI for BYE
        contact = headers.get("contact", "")
        contact_match = re.search(r'<([^>]+)>', contact)
        self._call_remote_uri = contact_match.group(1) if contact_match else ""

        # Parse SDP
        self._incoming_rtp_ip = ""
        self._incoming_rtp_port = 0
        if body:
            self._incoming_rtp_ip, self._incoming_rtp_port = self._parse_sdp(body)

        # Send 180 Ringing
        to_header = headers.get("to", "")
        ringing = (
            f"SIP/2.0 180 Ringing\r\n"
            f"Via: {via_header}\r\n"
            f"From: {from_header}\r\n"
            f"To: {to_header};tag={self._call_from_tag}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {self._incoming_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(ringing)

        self.in_call = True
        logger.info("Incoming SIP call from %s <%s>", caller_name, caller_num)
        if self._on_incoming_call:
            self._on_incoming_call("SIP", caller_num, caller_name)

    def _parse_blf_notify(self, body, from_header):
        """Parse dialog-info XML from NOTIFY for BLF state."""
        if not body:
            return
        # Extract extension from From header
        uri_match = re.search(r'sip:([^@>]+)@', from_header)
        extension = uri_match.group(1) if uri_match else ""
        if not extension:
            return

        # Simple XML parsing for dialog state
        if '<state>trying</state>' in body or '<state>early</state>' in body:
            state = "ringing"
        elif '<state>confirmed</state>' in body:
            state = "busy"
        elif '<state>terminated</state>' in body:
            state = "idle"
        else:
            # Check dialog-info state attribute
            if 'state="full"' in body or 'state="partial"' in body:
                if '<dialog ' in body:
                    state = "busy"
                else:
                    state = "idle"
            else:
                state = "unknown"

        logger.info("BLF %s -> %s", extension, state)
        if self._on_blf_state_change:
            self._on_blf_state_change(extension, state)

    def _re_register(self):
        """Periodic re-registration."""
        if self._running and self._server_addr:
            logger.info("SIP re-registration")
            self.register(self._server_addr[0], self._username,
                          self._password, self._server_addr[1])
