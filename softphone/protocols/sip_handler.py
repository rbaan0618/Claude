"""SIP protocol handler using PJSUA2.

This handler wraps the pjsua2 library (PJSIP's Python binding) to provide
SIP registration, calling, DTMF, hold/transfer, and BLF subscriptions.

Install: pip install pjsua2
Note: pjsua2 requires the PJSIP native library to be installed on the system.
On Windows, prebuilt wheels are available. On Linux: apt install python3-pjsua2
"""

import threading
import logging
from typing import Optional
from protocols.base import ProtocolHandler

logger = logging.getLogger(__name__)

try:
    import pjsua2 as pj
    PJSUA2_AVAILABLE = True
except ImportError:
    PJSUA2_AVAILABLE = False
    logger.warning("pjsua2 not installed. SIP functionality will run in simulation mode. "
                   "Install with: pip install pjsua2")


# ---------------------------------------------------------------------------
# PJSUA2 callback classes (only defined when the library is available)
# ---------------------------------------------------------------------------
if PJSUA2_AVAILABLE:

    class _AccountCallback(pj.Account):
        """Receives account-level events (registration, incoming calls)."""

        def __init__(self, handler: "SipHandler"):
            super().__init__()
            self._handler = handler

        def onRegState(self, prm):
            info = self.getInfo()
            is_registered = info.regIsActive
            self._handler.registered = is_registered
            status_text = "Registered" if is_registered else "Unregistered"
            logger.info("SIP registration: %s (code %s)", status_text, info.regStatus)
            if self._handler._on_registration_state:
                self._handler._on_registration_state("SIP", is_registered, info.regStatus)

        def onIncomingCall(self, prm):
            call = _CallCallback(self._handler, self, prm.callId)
            call_info = call.getInfo()
            remote_uri = call_info.remoteUri
            self._handler._current_call = call
            self._handler.in_call = True
            logger.info("Incoming SIP call from %s", remote_uri)
            if self._handler._on_incoming_call:
                self._handler._on_incoming_call("SIP", remote_uri, call_info.remoteContact)

    class _CallCallback(pj.Call):
        """Receives call-level events (state changes, media)."""

        def __init__(self, handler: "SipHandler", account, call_id=pj.PJSUA_INVALID_ID):
            super().__init__(account, call_id)
            self._handler = handler

        def onCallState(self, prm):
            info = self.getInfo()
            state = info.stateText
            logger.info("SIP call state: %s", state)
            if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._handler.in_call = False
                self._handler._current_call = None
            if self._handler._on_call_state_change:
                self._handler._on_call_state_change("SIP", state, info.lastReason)

        def onCallMediaState(self, prm):
            info = self.getInfo()
            for mi in info.media:
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and \
                   mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    aud_med = self.getAudioMedia(mi.index)
                    # Connect call audio to speaker and mic
                    ep = pj.Endpoint.instance()
                    aud_med.startTransmit(ep.audDevManager().getPlaybackDevMedia())
                    ep.audDevManager().getCaptureDevMedia().startTransmit(aud_med)

    class _BuddyCallback(pj.Buddy):
        """Receives presence/BLF events for a monitored extension."""

        def __init__(self, handler: "SipHandler", extension: str):
            super().__init__()
            self._handler = handler
            self._extension = extension

        def onBuddyState(self):
            info = self.getInfo()
            # Map PJSIP presence to BLF states
            if info.presStatus.status == pj.PJSUA_BUDDY_STATUS_ONLINE:
                state = "idle"
            elif info.presStatus.status == pj.PJSUA_BUDDY_STATUS_BUSY:
                state = "busy"
            else:
                state = "offline"
            logger.info("BLF %s -> %s", self._extension, state)
            if self._handler._on_blf_state_change:
                self._handler._on_blf_state_change(self._extension, state)


class SipHandler(ProtocolHandler):
    """SIP protocol handler using PJSUA2."""

    def __init__(self):
        super().__init__()
        self._endpoint = None
        self._account = None
        self._current_call = None
        self._buddies = {}
        self._transport = None
        self._config = {}
        self._sim_mode = not PJSUA2_AVAILABLE

    @property
    def protocol_name(self) -> str:
        return "SIP"

    def initialize(self, config: dict) -> bool:
        self._config = config
        if self._sim_mode:
            logger.info("SIP handler running in SIMULATION mode")
            return True
        try:
            self._endpoint = pj.Endpoint()
            ep_cfg = pj.EpConfig()
            ep_cfg.uaConfig.userAgent = "PySoftphone/1.0"
            ep_cfg.logConfig.level = 3
            ep_cfg.logConfig.consoleLevel = 3
            self._endpoint.libCreate()
            self._endpoint.libInit(ep_cfg)

            # Transport
            transport_type = pj.PJSIP_TRANSPORT_UDP
            if config.get("transport", "UDP").upper() == "TCP":
                transport_type = pj.PJSIP_TRANSPORT_TCP
            elif config.get("transport", "UDP").upper() == "TLS":
                transport_type = pj.PJSIP_TRANSPORT_TLS

            tp_cfg = pj.TransportConfig()
            tp_cfg.port = 0  # Auto-select port
            self._transport = self._endpoint.transportCreate(transport_type, tp_cfg)
            self._endpoint.libStart()
            logger.info("PJSUA2 SIP stack initialized")
            return True
        except Exception as e:
            logger.error("Failed to initialize SIP: %s", e)
            return False

    def register(self, server: str, username: str, password: str, port: int = 5060) -> bool:
        if self._sim_mode:
            self.registered = True
            logger.info("[SIM] SIP registered to %s as %s", server, username)
            if self._on_registration_state:
                self._on_registration_state("SIP", True, 200)
            return True
        try:
            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = f"sip:{username}@{server}"
            acc_cfg.regConfig.registrarUri = f"sip:{server}:{port}"

            cred = pj.AuthCredInfo("digest", "*", username, 0, password)
            acc_cfg.sipConfig.authCreds.append(cred)

            if self._config.get("display_name"):
                acc_cfg.idUri = f'"{ self._config["display_name"]}" <sip:{username}@{server}>'

            self._account = _AccountCallback(self)
            self._account.create(acc_cfg)
            logger.info("SIP registration sent to %s", server)
            return True
        except Exception as e:
            logger.error("SIP registration failed: %s", e)
            return False

    def unregister(self):
        if self._sim_mode:
            self.registered = False
            if self._on_registration_state:
                self._on_registration_state("SIP", False, 0)
            return
        if self._account:
            self._account.setRegistration(False)
            self.registered = False

    def make_call(self, uri: str) -> bool:
        if self._current_call:
            logger.warning("Already in a call")
            return False
        if self._sim_mode:
            self.in_call = True
            logger.info("[SIM] SIP calling %s", uri)
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "CALLING", "")
            return True
        if not self._account:
            logger.error("Not registered")
            return False
        try:
            # Normalize URI
            if not uri.startswith("sip:"):
                server = self._config.get("server", "")
                uri = f"sip:{uri}@{server}"
            call = _CallCallback(self, self._account)
            prm = pj.CallOpParam(True)
            call.makeCall(uri, prm)
            self._current_call = call
            self.in_call = True
            return True
        except Exception as e:
            logger.error("Failed to make SIP call: %s", e)
            return False

    def answer_call(self):
        if self._sim_mode:
            logger.info("[SIM] SIP call answered")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "CONFIRMED", "")
            return
        if self._current_call:
            prm = pj.CallOpParam()
            prm.statusCode = 200
            self._current_call.answer(prm)

    def hangup_call(self):
        if self._sim_mode:
            self.in_call = False
            logger.info("[SIM] SIP call hung up")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "DISCONNECTED", "Normal")
            return
        if self._current_call:
            prm = pj.CallOpParam()
            prm.statusCode = 603  # Decline
            self._current_call.hangup(prm)
            self._current_call = None
            self.in_call = False

    def send_dtmf(self, digit: str):
        if self._sim_mode:
            logger.info("[SIM] SIP DTMF: %s", digit)
            return
        if self._current_call:
            prm = pj.CallOpParam()
            self._current_call.dialDtmf(digit)

    def hold_call(self):
        if self._sim_mode:
            logger.info("[SIM] SIP call on hold")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "HOLD", "")
            return
        if self._current_call:
            prm = pj.CallOpParam()
            self._current_call.setHold(prm)

    def unhold_call(self):
        if self._sim_mode:
            logger.info("[SIM] SIP call resumed")
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "CONFIRMED", "")
            return
        if self._current_call:
            prm = pj.CallOpParam()
            prm.opt.flag = pj.PJSUA_CALL_UNHOLD
            self._current_call.reinvite(prm)

    def transfer_call(self, target: str):
        if self._sim_mode:
            logger.info("[SIM] SIP call transferred to %s", target)
            self.in_call = False
            if self._on_call_state_change:
                self._on_call_state_change("SIP", "DISCONNECTED", "Transfer")
            return
        if self._current_call:
            if not target.startswith("sip:"):
                server = self._config.get("server", "")
                target = f"sip:{target}@{server}"
            prm = pj.CallOpParam()
            self._current_call.xfer(target, prm)

    def subscribe_blf(self, extension: str):
        if self._sim_mode:
            logger.info("[SIM] BLF subscribed to %s", extension)
            return
        if not self._account:
            return
        server = self._config.get("server", "")
        buddy_cfg = pj.BuddyConfig()
        buddy_cfg.uri = f"sip:{extension}@{server}"
        buddy_cfg.subscribe = True
        buddy = _BuddyCallback(self, extension)
        buddy.create(self._account, buddy_cfg)
        self._buddies[extension] = buddy

    def shutdown(self):
        if self._sim_mode:
            self.registered = False
            self.in_call = False
            return
        self._buddies.clear()
        if self._current_call:
            try:
                prm = pj.CallOpParam()
                self._current_call.hangup(prm)
            except Exception:
                pass
        if self._account:
            self._account.shutdown()
        if self._endpoint:
            self._endpoint.libDestroy()
        self.registered = False
        self.in_call = False
        logger.info("SIP handler shut down")
