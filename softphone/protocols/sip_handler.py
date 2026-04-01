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


# ---------------------------------------------------------------------------
# Minimal STUN client (RFC 5389) for public IP/port discovery
# ---------------------------------------------------------------------------
STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.ekiga.net", 3478),
    ("stun.ideasip.com", 3478),
]

STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_RESPONSE = 0x0101
STUN_ATTR_MAPPED_ADDRESS = 0x0001
STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020
STUN_MAGIC_COOKIE = 0x2112A442


def stun_discover(local_sock=None, timeout=3):
    """Discover public IP and port via STUN. Returns (public_ip, public_port) or None."""
    own_sock = False
    if local_sock is None:
        local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        local_sock.bind(("", 0))
        own_sock = True

    old_timeout = local_sock.gettimeout()
    local_sock.settimeout(timeout)

    # Build STUN Binding Request
    txn_id = struct.pack("!III", random.getrandbits(32),
                         random.getrandbits(32), random.getrandbits(32))
    header = struct.pack("!HHI", STUN_BINDING_REQUEST, 0, STUN_MAGIC_COOKIE) + txn_id

    result = None
    for stun_server in STUN_SERVERS:
        try:
            local_sock.sendto(header, stun_server)
            data, addr = local_sock.recvfrom(1024)
            if len(data) < 20:
                continue

            # Parse response header
            msg_type, msg_len, magic = struct.unpack("!HHI", data[:8])
            if msg_type != STUN_BINDING_RESPONSE:
                continue

            # Parse attributes
            pos = 20
            while pos + 4 <= len(data):
                attr_type, attr_len = struct.unpack("!HH", data[pos:pos+4])
                attr_data = data[pos+4:pos+4+attr_len]

                if attr_type == STUN_ATTR_XOR_MAPPED_ADDRESS and attr_len >= 8:
                    family = attr_data[1]
                    if family == 0x01:  # IPv4
                        xport = struct.unpack("!H", attr_data[2:4])[0] ^ (STUN_MAGIC_COOKIE >> 16)
                        xip_int = struct.unpack("!I", attr_data[4:8])[0] ^ STUN_MAGIC_COOKIE
                        xip = socket.inet_ntoa(struct.pack("!I", xip_int))
                        result = (xip, xport)
                        break

                elif attr_type == STUN_ATTR_MAPPED_ADDRESS and attr_len >= 8:
                    family = attr_data[1]
                    if family == 0x01:
                        port = struct.unpack("!H", attr_data[2:4])[0]
                        ip = socket.inet_ntoa(attr_data[4:8])
                        result = (ip, port)
                        # Don't break — prefer XOR-MAPPED if found later

                # Align to 4-byte boundary
                pos += 4 + ((attr_len + 3) & ~3)

            if result:
                break
        except (socket.timeout, OSError):
            continue

    local_sock.settimeout(old_timeout)
    if own_sock:
        local_sock.close()
    return result


class RtpSession:
    """Handles RTP audio send/receive using pyaudio."""

    PCMU_PAYLOAD_TYPE = 0   # G.711 u-law
    DTMF_PAYLOAD_TYPE = 101  # RFC 2833 telephone-event
    SAMPLE_RATE = 8000
    FRAME_SIZE = 160  # 20ms at 8000Hz
    PTIME = 20  # ms

    def __init__(self, local_port, input_device=None, output_device=None):
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
        self._input_device = input_device
        self._output_device = output_device

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
                in_kwargs = dict(format=pyaudio.paInt16, channels=1,
                                 rate=self.SAMPLE_RATE, input=True,
                                 frames_per_buffer=self.FRAME_SIZE)
                if self._input_device is not None:
                    in_kwargs["input_device_index"] = self._input_device
                self._input_stream = self._pa.open(**in_kwargs)
                out_kwargs = dict(format=pyaudio.paInt16, channels=1,
                                  rate=self.SAMPLE_RATE, output=True,
                                  frames_per_buffer=self.FRAME_SIZE)
                if self._output_device is not None:
                    out_kwargs["output_device_index"] = self._output_device
                self._output_stream = self._pa.open(**out_kwargs)
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
        # NAT traversal — public IP/port discovered via STUN or Via rport
        self._public_ip = ""
        self._public_port = 0
        self._nat_detected = False
        # Authentication cache (reuse across REGISTER/INVITE)
        self._auth_realm = ""
        self._auth_nonce = ""
        self._auth_qop = ""
        self._auth_nc = 0
        self._auth_cached = False
        # Registration state
        self._reg_call_id = ""
        self._reg_cseq = 0
        self._reg_from_tag = ""
        self._reg_expires = 120
        self._reg_timer: Optional[threading.Timer] = None
        self._keepalive_timer: Optional[threading.Timer] = None
        self._keepalive_interval = 15  # seconds
        self._unregistering = False
        # Call state
        self._call_id = ""
        self._call_cseq = 0
        self._call_from_tag = ""
        self._call_to_tag = ""
        self._call_remote_uri = ""
        self._call_remote_target = ""
        self._call_route_set = []
        self._invite_auth_attempted = False  # True after we've sent one auth'd INVITE
        self._cached_sdp = ""   # Cached SDP body for INVITE re-send with auth
        self._rtp_session: Optional[RtpSession] = None
        self._rtp_port = 0
        self._on_hold = False
        self._hold_pending = False  # True while re-INVITE for hold/unhold is in flight
        # Attended transfer — saved original call state
        self._held_call = None  # dict with saved call state during consultation
        # BLF
        self._blf_subscriptions = {}

    @property
    def protocol_name(self) -> str:
        return "SIP"

    @property
    def on_hold(self) -> bool:
        return self._on_hold

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
        # Use local IP — let the server handle NAT rewriting via rport.
        # Asterisk with nat=force_rport,comedia rewrites Contact itself;
        # putting a STUN public IP here confuses many servers.
        return f"<sip:{self._username}@{self._local_ip}:{self._local_port}>"

    def _send_sip(self, message):
        """Send a SIP message to the server."""
        if self._sock and self._server_addr:
            data = message.encode("utf-8")
            try:
                self._sock.sendto(data, self._server_addr)
                # Log full message for debugging
                first_line = message.split("\r\n", 1)[0]
                logger.debug("SIP TX >>> %s:%d (%d bytes)\n%s",
                             self._server_addr[0], self._server_addr[1],
                             len(data), message.rstrip())
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

    def _make_digest_response(self, method, uri, realm, nonce, username, password,
                               qop=None, cnonce=None, nc=None):
        """Compute SIP digest authentication response (RFC 2617)."""
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop == "auth" and cnonce and nc:
            response = hashlib.md5(
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return response

    def _build_auth_header(self, method, uri, auth_header, header_name="Authorization"):
        """Build a Digest authorization header and cache realm/nonce."""
        params = self._extract_auth_params(auth_header)
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        qop = params.get("qop", "")
        # Cache for reuse on subsequent requests (INVITE, unregister, etc)
        self._auth_realm = realm
        self._auth_nonce = nonce
        self._auth_qop = qop
        self._auth_nc = 1
        self._auth_cached = True

        if qop == "auth":
            cnonce = f"{random.randint(10000000, 99999999):08x}"
            nc = f"{self._auth_nc:08d}"
            self._auth_nc += 1
            response = self._make_digest_response(method, uri, realm, nonce,
                                                   self._username, self._password,
                                                   qop, cnonce, nc)
            return (f'{header_name}: Digest username="{self._username}", '
                    f'realm="{realm}", nonce="{nonce}", uri="{uri}", '
                    f'response="{response}", algorithm=MD5, '
                    f'cnonce="{cnonce}", qop={qop}, nc={nc}')
        else:
            response = self._make_digest_response(method, uri, realm, nonce,
                                                   self._username, self._password)
            return (f'{header_name}: Digest username="{self._username}", '
                    f'realm="{realm}", nonce="{nonce}", uri="{uri}", '
                    f'response="{response}", algorithm=MD5')

    def _build_cached_auth_header(self, method, uri, header_name="Authorization"):
        """Build auth header using cached realm/nonce from prior 401."""
        if not self._auth_cached:
            return None
        qop = getattr(self, '_auth_qop', '')
        if qop == "auth":
            cnonce = f"{random.randint(10000000, 99999999):08x}"
            nc = f"{getattr(self, '_auth_nc', 1):08d}"
            self._auth_nc = getattr(self, '_auth_nc', 1) + 1
            response = self._make_digest_response(method, uri, self._auth_realm,
                                                   self._auth_nonce,
                                                   self._username, self._password,
                                                   qop, cnonce, nc)
            return (f'{header_name}: Digest username="{self._username}", '
                    f'realm="{self._auth_realm}", nonce="{self._auth_nonce}", uri="{uri}", '
                    f'response="{response}", algorithm=MD5, '
                    f'cnonce="{cnonce}", qop={qop}, nc={nc}')
        else:
            response = self._make_digest_response(method, uri, self._auth_realm,
                                                   self._auth_nonce,
                                                   self._username, self._password)
            return (f'{header_name}: Digest username="{self._username}", '
                    f'realm="{self._auth_realm}", nonce="{self._auth_nonce}", uri="{uri}", '
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
        """Build SDP offer with audio on our RTP port.

        Uses the public (NAT-discovered) IP so the remote end can send
        RTP back to us through the firewall.
        """
        rtp_port = self._rtp_port or (self._local_port + 2)
        # Discover public RTP port via STUN if behind NAT
        sdp_ip = self._public_ip or self._local_ip
        if self._nat_detected and not self._rtp_port:
            # Use STUN to find public mapping for our RTP port
            try:
                rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                rtp_sock.bind(("", rtp_port))
                actual_rtp_port = rtp_sock.getsockname()[1]
                stun_result = stun_discover(rtp_sock)
                rtp_sock.close()
                if stun_result:
                    sdp_ip = stun_result[0]
                    rtp_port = stun_result[1]
                    logger.info("STUN discovered RTP: %s:%d", sdp_ip, rtp_port)
                else:
                    rtp_port = actual_rtp_port
            except Exception as e:
                logger.warning("STUN for RTP failed: %s — using local", e)

        sdp = (
            "v=0\r\n"
            f"o=pysoftphone 0 0 IN IP4 {sdp_ip}\r\n"
            "s=MyLineTelecom\r\n"
            f"c=IN IP4 {sdp_ip}\r\n"
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

    def initialize(self, config: dict, audio_config: dict = None) -> bool:
        self._config = config
        self._audio_config = audio_config or {}
        self._use_rport = config.get("rport", True)
        self._local_port = int(config.get("local_port", 5060)) or 5060
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.settimeout(1.0)
            self._sock.bind(("", self._local_port))
            actual = self._sock.getsockname()[1]
            self._local_port = actual

            # Discover public IP/port via STUN before starting
            logger.info("Running STUN discovery for NAT traversal...")
            stun_result = stun_discover(self._sock)
            if stun_result:
                self._public_ip, self._public_port = stun_result
                self._nat_detected = (self._public_ip != self._local_ip)
                logger.info("STUN: public address %s:%d (NAT %s)",
                            self._public_ip, self._public_port,
                            "detected" if self._nat_detected else "not detected")
            else:
                logger.warning("STUN discovery failed — NAT traversal may not work")

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
        if not self._sock or not self._running:
            logger.error("SIP not initialized — cannot register")
            return False
        self._server_addr = (server, port)
        self._username = username
        self._password = password
        self._display_name = self._config.get("display_name", username)
        self._local_ip = self._get_local_ip(server)

        # Run STUN now that we know our local IP (if not already discovered)
        if not self._public_ip and self._sock:
            logger.info("Running STUN discovery...")
            stun_result = stun_discover(self._sock)
            if stun_result:
                self._public_ip, self._public_port = stun_result
                self._nat_detected = (self._public_ip != self._local_ip)
                logger.info("STUN: public %s:%d (NAT %s)",
                            self._public_ip, self._public_port,
                            "detected" if self._nat_detected else "not detected")

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
            f"User-Agent: MyLineTelecom/1.0\r\n"
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
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Expires: {self._reg_expires}\r\n"
            f"{auth_line}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REGISTER with auth sent")

    def _send_unregister_with_auth(self, auth_header):
        """Re-send REGISTER Expires=0 with digest authentication."""
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
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Expires: 0\r\n"
            f"{auth_line}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REGISTER Expires=0 with auth sent")

    def unregister(self):
        if not self._server_addr:
            return
        self._unregistering = True
        server, port = self._server_addr
        self._reg_cseq += 1
        request_uri = f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_uri = f"<sip:{self._username}@{server}>"

        # Include cached auth if available (server will 401 otherwise)
        auth_line = ""
        if self._auth_cached:
            auth_hdr = self._build_cached_auth_header("REGISTER", request_uri)
            if auth_hdr:
                auth_line = f"{auth_hdr}\r\n"

        msg = (
            f"REGISTER {request_uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._reg_from_tag}\r\n"
            f"To: {to_uri}\r\n"
            f"Call-ID: {self._reg_call_id}\r\n"
            f"CSeq: {self._reg_cseq} REGISTER\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Expires: 0\r\n"
            f"{auth_line}"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP REGISTER Expires=0 (unregister) sent")
        if self._reg_timer:
            self._reg_timer.cancel()

    def make_call(self, uri: str) -> bool:
        if self.in_call:
            logger.warning("Already in a call")
            return False
        if not self._server_addr:
            logger.error("Not registered")
            return False

        server, port = self._server_addr
        # Normalize URI — always route through server as proxy
        if not uri.startswith("sip:"):
            uri = f"sip:{uri}@{server}"
        self._call_remote_uri = uri

        self._call_id = _generate_call_id()
        self._call_from_tag = _generate_tag()
        self._call_to_tag = ""
        self._call_cseq = 1
        self._invite_auth_attempted = False

        sdp_body, rtp_port = self._build_sdp()
        self._rtp_port = rtp_port
        self._cached_sdp = sdp_body  # Cache for auth retry
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
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY, REFER\r\n"
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

    def _send_invite_with_auth(self, auth_header, is_proxy=False):
        """Re-send INVITE with digest auth (new CSeq, new Via branch)."""
        server, port = self._server_addr
        self._call_cseq += 1
        # Reuse the same SDP from the original INVITE to keep ports consistent
        sdp_body = self._cached_sdp
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        uri = self._call_remote_uri

        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        auth_line = self._build_auth_header("INVITE", uri, auth_header, header_name)

        msg = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY, REFER\r\n"
            f"{auth_line}\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        self._send_sip(msg)
        logger.info("SIP INVITE with auth sent (CSeq %d)", self._call_cseq)

    def _send_ack(self, to_tag=""):
        """Send ACK for an INVITE transaction."""
        self._send_ack_for_call(self._call_id, self._call_from_tag, to_tag,
                                 self._call_remote_uri)

    def _send_ack_for_call(self, call_id, from_tag, to_tag="", remote_uri=""):
        """Send ACK for a specific call (used for held call re-INVITE too)."""
        server, port = self._server_addr
        uri = remote_uri or f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_header = f"<{uri}>"
        if to_tag:
            to_header += f";tag={to_tag}"

        msg = (
            f"ACK {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {call_id}\r\n"
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
            f"User-Agent: MyLineTelecom/1.0\r\n"
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

    def _build_hold_sdp(self):
        """Build SDP with a=sendonly for hold."""
        sdp_ip = self._public_ip or self._local_ip
        rtp_port = self._rtp_port or (self._local_port + 2)
        sdp = (
            "v=0\r\n"
            f"o=pysoftphone 0 1 IN IP4 {sdp_ip}\r\n"
            "s=MyLineTelecom\r\n"
            f"c=IN IP4 {sdp_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=ptime:20\r\n"
            "a=sendonly\r\n"
        )
        return sdp

    def _build_unhold_sdp(self):
        """Build SDP with a=sendrecv for unhold."""
        sdp_ip = self._public_ip or self._local_ip
        rtp_port = self._rtp_port or (self._local_port + 2)
        sdp = (
            "v=0\r\n"
            f"o=pysoftphone 0 2 IN IP4 {sdp_ip}\r\n"
            "s=MyLineTelecom\r\n"
            f"c=IN IP4 {sdp_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        return sdp

    def _send_reinvite(self, sdp_body):
        """Send a re-INVITE with the given SDP body (for hold/unhold)."""
        if not self._call_id or not self._server_addr:
            return
        server, port = self._server_addr
        self._call_cseq += 1
        uri = self._call_remote_uri or f"sip:{server}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_header = f"<{uri}>"
        if self._call_to_tag:
            to_header += f";tag={self._call_to_tag}"

        msg = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={self._call_from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self._call_id}\r\n"
            f"CSeq: {self._call_cseq} INVITE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        # Add cached auth if available
        if self._auth_cached:
            auth_line = self._build_cached_auth_header("INVITE", uri)
            msg = msg.replace("Max-Forwards: 70\r\n",
                              f"Max-Forwards: 70\r\n{auth_line}\r\n")
        self._send_sip(msg)

    def hold_call(self):
        """Put the current call on hold via re-INVITE with sendonly SDP."""
        if self._hold_pending:
            logger.debug("Hold re-INVITE already pending, ignoring")
            return
        self._on_hold = True
        self._hold_pending = True
        if self._rtp_session:
            self._rtp_session.set_muted(True)
        # Send re-INVITE with sendonly to tell server to play MOH
        sdp = self._build_hold_sdp()
        self._send_reinvite(sdp)
        logger.info("SIP hold — re-INVITE with sendonly sent")
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "HOLD", "")

    def unhold_call(self):
        """Resume the current call via re-INVITE with sendrecv SDP."""
        if self._hold_pending:
            logger.debug("Hold re-INVITE already pending, ignoring")
            return
        self._on_hold = False
        self._hold_pending = True
        if self._rtp_session:
            self._rtp_session.set_muted(False)
        # Send re-INVITE with sendrecv to resume audio
        sdp = self._build_unhold_sdp()
        self._send_reinvite(sdp)
        logger.info("SIP unhold — re-INVITE with sendrecv sent")
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
        # Add cached auth if available
        if self._auth_cached:
            auth_line = self._build_cached_auth_header("REFER", uri)
            msg = msg.replace("Max-Forwards: 70\r\n",
                              f"Max-Forwards: 70\r\n{auth_line}\r\n")
        self._send_sip(msg)
        logger.info("SIP REFER sent to transfer to %s", target)

    # -- Attended transfer (consultation) ------------------------------------

    def consultation_call(self, target: str):
        """Save current call on hold and start a new consultation call."""
        if not self._call_id or not self._server_addr:
            return
        server, _ = self._server_addr
        # Save the current (held) call state
        consult_uri = target if target.startswith("sip:") else f"sip:{target}@{server}"
        self._held_call = {
            "call_id": self._call_id,
            "call_cseq": self._call_cseq,
            "call_from_tag": self._call_from_tag,
            "call_to_tag": self._call_to_tag,
            "call_remote_uri": self._call_remote_uri,
            "rtp_session": self._rtp_session,
            "rtp_port": self._rtp_port,
            "invite_auth_attempted": self._invite_auth_attempted,
            "transfer_target": consult_uri,
        }
        # Clear call state for the new consultation call
        self._rtp_session = None
        self._hold_pending = False
        # Use a different RTP port for the consultation call (offset from held call)
        held_rtp = self._rtp_port
        self._rtp_port = held_rtp + 2 if held_rtp else 0
        self.in_call = False
        self._call_id = ""
        self._invite_auth_attempted = False
        # Make the consultation call
        self.make_call(target)
        logger.info("Consultation call started to %s (original call on hold)", target)

    def complete_attended_transfer(self):
        """Complete attended transfer: REFER held call to consultation target, hang up consultation."""
        if not self._held_call:
            logger.warning("No held call to transfer")
            return
        transfer_target = self._held_call["transfer_target"]

        # Hang up the consultation call
        self.hangup_call()

        # Restore the held call state
        held = self._held_call
        self._call_id = held["call_id"]
        self._call_cseq = held["call_cseq"]
        self._call_from_tag = held["call_from_tag"]
        self._call_to_tag = held["call_to_tag"]
        self._call_remote_uri = held["call_remote_uri"]
        self._rtp_session = held["rtp_session"]
        self._rtp_port = held["rtp_port"]
        self.in_call = True

        # Send REFER on the original call to transfer it to the consultation target
        self.transfer_call(transfer_target)
        logger.info("Attended transfer completed — REFER sent to %s", transfer_target)

        # Clean up — the REFER will cause the server to bridge the calls
        self._stop_rtp()
        self.in_call = False
        self._call_id = ""
        self._held_call = None
        self._on_hold = False
        if self._on_call_state_change:
            self._on_call_state_change("SIP", "DISCONNECTED", "Transfer completed")

    def cancel_consultation(self):
        """Cancel consultation call and resume the original held call."""
        if not self._held_call:
            logger.warning("No held call to resume")
            return
        # Hang up the consultation call
        self.hangup_call()

        # Restore the held call
        held = self._held_call
        self._call_id = held["call_id"]
        self._call_cseq = held["call_cseq"]
        self._call_from_tag = held["call_from_tag"]
        self._call_to_tag = held["call_to_tag"]
        self._call_remote_uri = held["call_remote_uri"]
        self._rtp_session = held["rtp_session"]
        self._rtp_port = held["rtp_port"]
        self.in_call = True
        self._held_call = None

        # Unhold — resume audio
        self.unhold_call()
        logger.info("Consultation cancelled, original call resumed")

    def subscribe_blf(self, extension: str):
        """Subscribe to dialog event package for BLF."""
        if not self._server_addr:
            return
        server, port = self._server_addr
        sub_call_id = _generate_call_id()
        sub_tag = _generate_tag()
        self._blf_subscriptions[extension] = {
            "call_id": sub_call_id, "tag": sub_tag, "cseq": 1,
            "auth_attempted": False,
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
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Event: dialog\r\n"
            f"Accept: application/dialog-info+xml\r\n"
            f"Expires: 3600\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP SUBSCRIBE (BLF) sent for %s", extension)

    def _send_subscribe_with_auth(self, extension, auth_header):
        """Re-send SUBSCRIBE with digest authentication."""
        if extension not in self._blf_subscriptions:
            return
        sub = self._blf_subscriptions[extension]
        sub["cseq"] += 1
        server, port = self._server_addr
        uri = f"sip:{extension}@{server}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'

        is_proxy = "proxy" not in auth_header.lower()  # detect from response handler
        header_name = "Proxy-Authorization" if sub.get("is_proxy") else "Authorization"
        auth_line = self._build_auth_header("SUBSCRIBE", uri, auth_header, header_name)

        msg = (
            f"SUBSCRIBE {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={sub['tag']}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {sub['call_id']}\r\n"
            f"CSeq: {sub['cseq']} SUBSCRIBE\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Event: dialog\r\n"
            f"Accept: application/dialog-info+xml\r\n"
            f"Expires: 3600\r\n"
            f"{auth_line}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.info("SIP SUBSCRIBE (BLF) with auth sent for %s", extension)

    def shutdown(self):
        self._running = False
        self._stop_keepalive()
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
        input_dev = self._audio_config.get("input_device", "")
        output_dev = self._audio_config.get("output_device", "")
        in_idx = int(input_dev) if input_dev not in ("", None) else None
        out_idx = int(output_dev) if output_dev not in ("", None) else None
        self._rtp_session = RtpSession(self._rtp_port, input_device=in_idx,
                                        output_device=out_idx)
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
        text = data.decode("utf-8", errors="replace")
        logger.debug("SIP RX <<< %s:%d (%d bytes)\n%s",
                      addr[0], addr[1], len(data), text.rstrip())
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
            # Accept responses for the current call OR the held call (re-INVITE for hold/unhold)
            held_call_id = self._held_call["call_id"] if self._held_call else ""
            if call_id and self._call_id and call_id != self._call_id:
                if call_id == held_call_id and status_code == 200:
                    # 200 OK for the held call's re-INVITE (hold) — just ACK it
                    to_tag = self._extract_to_tag(headers)
                    self._send_ack_for_call(call_id, self._held_call["call_from_tag"],
                                            to_tag, self._held_call["call_remote_uri"])
                    logger.debug("ACK sent for held call re-INVITE 200 OK")
                    return
                logger.debug("Ignoring INVITE response for old Call-ID %s", call_id)
                return
            self._handle_invite_response(status_code, headers, body)
        elif "BYE" in cseq:
            logger.info("BYE response: %d", status_code)
        elif "REFER" in cseq:
            logger.info("REFER response: %d", status_code)
        elif "SUBSCRIBE" in cseq:
            self._handle_subscribe_response(status_code, headers, body, call_id)
        elif "OPTIONS" in cseq:
            pass  # keepalive response — no action needed

    def _extract_to_tag(self, headers):
        """Extract tag from To header."""
        to_header = headers.get("to", "")
        tag_match = re.search(r'tag=([^;>\s]+)', to_header)
        return tag_match.group(1) if tag_match else ""

    def _parse_via_nat(self, headers):
        """Extract received= and rport= from Via header for NAT discovery."""
        via = headers.get("via", "")
        if isinstance(via, list):
            via = via[0]
        received_match = re.search(r'received=([^;,\s]+)', via)
        rport_match = re.search(r'rport=(\d+)', via)
        if received_match:
            public_ip = received_match.group(1)
            public_port = int(rport_match.group(1)) if rport_match else self._local_port
            if public_ip != self._local_ip or public_port != self._local_port:
                self._public_ip = public_ip
                self._public_port = public_port
                self._nat_detected = True
                logger.info("NAT detected via rport: public %s:%d (local %s:%d)",
                            public_ip, public_port, self._local_ip, self._local_port)
            return public_ip, public_port
        return None, None

    def _handle_register_response(self, status_code, headers, body):
        if status_code == 200:
            # Parse Via for NAT-discovered public address
            self._parse_via_nat(headers)

            if self._unregistering:
                # Successful unregister
                self._unregistering = False
                self.registered = False
                self._stop_keepalive()
                logger.info("SIP unregistered successfully")
                if self._on_registration_state:
                    self._on_registration_state("SIP", False, 0)
            else:
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
                # Start keepalive pings
                self._start_keepalive()

        elif status_code == 401 or status_code == 407:
            # Authentication required
            auth_header = headers.get("www-authenticate", "")
            if not auth_header:
                auth_header = headers.get("proxy-authenticate", "")
            if auth_header:
                if isinstance(auth_header, list):
                    auth_header = auth_header[0]
                if self._unregistering:
                    # Re-send unregister with fresh auth
                    self._send_unregister_with_auth(auth_header)
                else:
                    self._send_register_with_auth(auth_header)
            else:
                logger.error("SIP 401 but no auth header")
                self._unregistering = False
                if self._on_registration_state:
                    self._on_registration_state("SIP", False, status_code)

        elif status_code == 403:
            logger.error("SIP registration forbidden (403)")
            self._unregistering = False
            if self._on_registration_state:
                self._on_registration_state("SIP", False, 403)

        else:
            logger.warning("SIP REGISTER response: %d", status_code)
            self._unregistering = False
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

            # Send ACK (always, even for retransmitted 200s)
            self._send_ack(self._call_to_tag)

            # Re-INVITE 200 OK (hold/unhold) — RTP already exists, just ACK
            if self._rtp_session and self.in_call:
                self._hold_pending = False
                logger.debug("Re-INVITE 200 OK acknowledged (hold/unhold)")
                return

            # Late 200 OK after BYE — ignore
            if not self.in_call:
                logger.debug("Ignoring late 200 OK (call ended)")
                return

            # Parse SDP for remote RTP address
            if body:
                rtp_ip, rtp_port = self._parse_sdp(body)
                if rtp_ip and rtp_port:
                    self._start_rtp(rtp_ip, rtp_port)

            logger.info("SIP call connected")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "CONFIRMED", "")

        elif status_code == 401 or status_code == 407:
            # Need auth for INVITE — only retry ONCE then give up
            if self._invite_auth_attempted:
                # Already sent an auth'd INVITE — ignore retransmissions
                logger.debug("Ignoring retransmitted %d (auth already sent)", status_code)
                return

            self._invite_auth_attempted = True
            # Determine which auth header type was sent by the server
            is_proxy = False
            auth_header = headers.get("proxy-authenticate", "")
            if auth_header:
                is_proxy = True
            else:
                auth_header = headers.get("www-authenticate", "")
            if auth_header:
                if isinstance(auth_header, list):
                    auth_header = auth_header[0]
                # ACK the 407 first (required by RFC 3261)
                to_tag = self._extract_to_tag(headers)
                self._send_ack(to_tag)
                self._send_invite_with_auth(auth_header, is_proxy)
            else:
                logger.error("SIP %d but no auth header found", status_code)
                self._send_ack(self._extract_to_tag(headers))
                self.in_call = False
                self._call_id = ""
                if self._on_call_state_change:
                    self._on_call_state_change("SIP", "REJECTED", "No auth challenge")

        elif status_code == 486 or status_code == 600:
            to_tag = self._extract_to_tag(headers)
            self._send_ack(to_tag)
            if self._rtp_session:
                # Re-INVITE error (hold/unhold) — don't kill the call
                self._hold_pending = False
                logger.warning("Re-INVITE rejected: %d (call continues)", status_code)
            else:
                self.in_call = False
                self._call_id = ""
                if self._on_call_state_change:
                    self._on_call_state_change("SIP", "BUSY", "Busy")

        elif status_code == 503:
            to_tag = self._extract_to_tag(headers)
            self._send_ack(to_tag)
            if self._rtp_session:
                self._hold_pending = False
                logger.warning("Re-INVITE 503 (call continues)")
            else:
                self.in_call = False
                self._call_id = ""
                logger.warning("SIP 503 Service Unavailable — destination unreachable")
                if self._on_call_state_change:
                    self._on_call_state_change("SIP", "REJECTED", "503 Service Unavailable")

        elif status_code >= 400:
            to_tag = self._extract_to_tag(headers)
            self._send_ack(to_tag)
            if self._rtp_session:
                # Re-INVITE error (hold/unhold) — don't kill the call
                self._hold_pending = False
                logger.warning("Re-INVITE rejected: %d (call continues)", status_code)
            else:
                self.in_call = False
                self._call_id = ""
                logger.warning("SIP INVITE rejected: %d", status_code)
                if self._on_call_state_change:
                    self._on_call_state_change("SIP", "REJECTED", str(status_code))

    def _handle_subscribe_response(self, status_code, headers, body, call_id):
        """Handle responses to SUBSCRIBE requests (BLF)."""
        if status_code == 200:
            logger.info("SUBSCRIBE accepted")
        elif status_code == 401 or status_code == 407:
            # Find which subscription this belongs to by Call-ID
            extension = None
            for ext, sub in self._blf_subscriptions.items():
                if sub["call_id"] == call_id:
                    extension = ext
                    break
            if not extension:
                logger.warning("SUBSCRIBE %d for unknown Call-ID %s", status_code, call_id)
                return
            sub = self._blf_subscriptions[extension]
            if sub.get("auth_attempted"):
                logger.warning("SUBSCRIBE auth failed for %s", extension)
                return
            sub["auth_attempted"] = True
            # Get auth header
            is_proxy = False
            auth_header = headers.get("proxy-authenticate", "")
            if auth_header:
                is_proxy = True
            else:
                auth_header = headers.get("www-authenticate", "")
            if auth_header:
                if isinstance(auth_header, list):
                    auth_header = auth_header[0]
                sub["is_proxy"] = is_proxy
                self._send_subscribe_with_auth(extension, auth_header)
            else:
                logger.error("SUBSCRIBE %d but no auth header", status_code)
        else:
            logger.info("SUBSCRIBE response: %d", status_code)

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
                f"User-Agent: MyLineTelecom/1.0\r\n"
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

    # -- Keepalive ---------------------------------------------------------------

    def _start_keepalive(self):
        """Start periodic OPTIONS keepalive pings."""
        self._stop_keepalive()
        self._send_keepalive()

    def _stop_keepalive(self):
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None

    def _send_keepalive(self):
        """Send OPTIONS keepalive and schedule next."""
        if not self._running or not self._server_addr or not self.registered:
            return
        server, port = self._server_addr
        uri = f"sip:{server}:{port}"
        from_uri = f'"{self._display_name}" <sip:{self._username}@{server}>'
        to_uri = f"<sip:{server}>"
        call_id = _generate_call_id()

        msg = (
            f"OPTIONS {uri} SIP/2.0\r\n"
            f"Via: {self._via_header()}\r\n"
            f"From: {from_uri};tag={_generate_tag()}\r\n"
            f"To: {to_uri}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: 1 OPTIONS\r\n"
            f"Contact: {self._contact_header()}\r\n"
            f"Max-Forwards: 70\r\n"
            f"User-Agent: MyLineTelecom/1.0\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send_sip(msg)
        logger.debug("SIP keepalive OPTIONS sent")

        # Schedule next keepalive
        self._keepalive_timer = threading.Timer(
            self._keepalive_interval, self._send_keepalive)
        self._keepalive_timer.daemon = True
        self._keepalive_timer.start()
