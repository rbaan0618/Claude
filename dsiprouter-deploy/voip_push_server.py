#!/usr/bin/env python3
"""
MyLine VoIP Push Server
=======================
Receives a POST from Kamailio (route[SENDPUSH]) and sends an Apple VoIP
push notification via APNs HTTP/2.  This wakes a suspended iPhone so its
SIP stack can re-register and accept the pending INVITE.

Listens on:    http://127.0.0.1:8070
Trigger from:  kamailio.cfg route[SENDPUSH] uses
               http_async_query("http://127.0.0.1:8070/push", "PUSH_CB")

Deploy path:   /opt/myline/voip_push_server.py
Service unit:  /etc/systemd/system/voip-push.service  (see voip-push.service)

Dependencies:
    apt install python3-cryptography curl
"""

import json, time, base64, subprocess, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# ============================================================
# CONFIGURATION — fill in before deploy
# ============================================================

# Path to your APNs Auth Key (.p8) on the server. Generate at:
# https://developer.apple.com/account/resources/authkeys/list
# Apple downloads it ONCE — store it securely (chmod 600, root:root).
APNS_KEY_FILE  = '/etc/myline/AuthKey_YOURKEYID.p8'

# Key ID — 10-character string shown next to the key in Apple Dev portal.
APNS_KEY_ID    = 'YOUR_APNS_KEY_ID'

# Team ID — 10-character string shown top-right of Apple Dev portal.
APNS_TEAM_ID   = 'YOUR_APPLE_TEAM_ID'

# App bundle identifier (same as Xcode → Target → Bundle Identifier).
APNS_BUNDLE_ID = 'com.yourcompany.softphone'

# Shared secret — kamailio's route[SENDPUSH] must include this in every
# POST body.  Generate with: openssl rand -hex 32
PUSH_SECRET    = 'YOUR_SHARED_SECRET_HERE'

# APNs endpoint:
#   https://api.push.apple.com         — production (App Store / TestFlight builds)
#   https://api.sandbox.push.apple.com — development (Xcode debug builds)
APNS_HOST      = 'https://api.push.apple.com'

# HTTP listen port (must match http_async_query URL in kamailio.cfg).
LISTEN_PORT    = 8070

# ============================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('voip_push')


def build_jwt():
    """
    Build an APNs JWT signed with the .p8 ES256 private key.
    Apple accepts each JWT for up to 1 hour; we build a fresh one per push
    (negligible cost — ECDSA-P256 sign is microseconds).
    """
    with open(APNS_KEY_FILE, 'rb') as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    header  = base64.urlsafe_b64encode(json.dumps({'alg': 'ES256', 'kid': APNS_KEY_ID}).encode()).rstrip(b'=')
    payload = base64.urlsafe_b64encode(json.dumps({'iss': APNS_TEAM_ID, 'iat': int(time.time())}).encode()).rstrip(b'=')
    signing = header + b'.' + payload
    sig = private_key.sign(signing, ec.ECDSA(hashes.SHA256()))
    # APNs expects the raw r||s (64 bytes), not the DER-encoded ECDSA signature.
    r, s = decode_dss_signature(sig)
    raw = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    return (signing + b'.' + base64.urlsafe_b64encode(raw).rstrip(b'=')).decode()


def send_push(token, caller, caller_name):
    """
    POST a VoIP push to APNs HTTP/2.  Uses curl rather than `requests`
    because Python's stdlib has no HTTP/2 and APNs requires it.
    Returns the curl stdout (response body + HTTP code on a new line).
    """
    jwt   = build_jwt()
    topic = APNS_BUNDLE_ID + '.voip'   # PushKit topic = bundle.voip
    body  = json.dumps({
        'caller':     caller,
        'callerName': caller_name,
        'aps':        {'content-available': 1},   # required for background wakeup
    })
    cmd = [
        'curl', '-s', '-w', '\n%{http_code}',
        '--http2', '-X', 'POST',
        '-H', f'Authorization: bearer {jwt}',
        '-H', f'apns-topic: {topic}',
        '-H', 'apns-push-type: voip',
        '-H', 'apns-priority: 10',         # 10 = deliver immediately (required for VoIP)
        '-H', 'apns-expiration: 0',        # 0 = do NOT store if device offline
        '-H', 'Content-Type: application/json',
        '-d', body,
        f'{APNS_HOST}/3/device/{token}',
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


class PushHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"error":"bad json"}'); return

        if data.get('secret') != PUSH_SECRET:
            self.send_response(403); self.end_headers()
            self.wfile.write(b'{"error":"forbidden"}'); return

        token       = data.get('token', '')
        caller      = data.get('caller', 'Unknown')
        caller_name = data.get('caller_name', '')

        if not token:
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"error":"missing token"}'); return

        log.info(f'Push -> token={token[:8]}... caller={caller} name={caller_name}')
        try:
            result = send_push(token, caller, caller_name)
            log.info(f'APNs: {result}')
            resp = json.dumps({'status': 'sent', 'apns': result}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(resp)
        except Exception as e:
            log.error(f'Push failed: {e}')
            self.send_response(500); self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def log_message(self, *a):
        # Suppress default access-log noise — we log meaningful events only.
        pass


if __name__ == '__main__':
    log.info(f'VoIP Push Server on 127.0.0.1:{LISTEN_PORT}')
    HTTPServer(('127.0.0.1', LISTEN_PORT), PushHandler).serve_forever()
