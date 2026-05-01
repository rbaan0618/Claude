package com.mylinetelecom.softphone.sip

import android.util.Log
import com.mylinetelecom.softphone.models.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.security.MessageDigest
import kotlin.random.Random

class SipHandler(
    private val scope: CoroutineScope
) {
    companion object {
        private const val TAG = "SipHandler"
        private const val REGISTER_EXPIRES = 300
        private const val KEEPALIVE_INTERVAL = 10_000L  // 10s — faster network-loss detection
        private const val RE_REGISTER_BEFORE = 30_000L
    }

    // State flows
    private val _registrationState = MutableStateFlow(RegistrationState.UNREGISTERED)
    val registrationState: StateFlow<RegistrationState> = _registrationState

    private val _callState = MutableStateFlow(CallState.IDLE)
    val callState: StateFlow<CallState> = _callState

    private val _remoteNumber = MutableStateFlow("")
    val remoteNumber: StateFlow<String> = _remoteNumber

    private val _remoteName = MutableStateFlow("")
    val remoteName: StateFlow<String> = _remoteName

    private val _blfStates = MutableStateFlow<Map<String, BlfState>>(emptyMap())
    val blfStates: StateFlow<Map<String, BlfState>> = _blfStates

    // SIP config
    private var config = SipConfig()
    private var socket: DatagramSocket? = null
    private var localIp: String = "0.0.0.0"
    private var publicIp: String = ""
    private var publicPort: Int = 0

    // Registration state
    private var registerCallId = ""
    private var registerFromTag = ""
    private var registerCseq = 1

    // Auth state
    private var authRealm = ""
    private var authNonce = ""
    private var authOpaque = ""
    private var authAlgorithm = "MD5"
    private var authQop = ""
    private var authNonceCount = 0
    private var authCNonce = ""
    private var inviteAuthAttempted = false

    // RTP
    private var rtpSession: RtpSession? = null
    private var rtpSocket: DatagramSocket? = null
    private var localRtpPort = 0
    private var remoteRtpHost = ""
    private var remoteRtpPort = 0
    private var negotiatedCodec = 0 // 0=PCMU, 3=GSM (from remote SDP)

    // Current Call tracking
    private var currentCallId = ""
    private var currentLocalTag = ""
    private var currentRemoteTag = ""
    private var currentRemoteUri = ""
    private var currentCallDirection = "" // "inbound" or "outbound"
    private var currentCSeq = 1
    private var currentInviteBranch = "" // Via branch of the current INVITE (for CANCEL)
    private var incomingInviteMsg: String? = null
    private var pendingHoldMode = false // Track if re-INVITE is for hold
    private var pendingReferTarget = "" // Track blind transfer target for auth retry
    private var referAuthAttempted = false

    // Attended transfer / consultation call tracking
    private var consultCallId = ""
    private var consultLocalTag = ""
    private var consultRemoteTag = ""
    private var consultRemoteUri = ""
    private var consultCSeq = 1
    private var consultInviteBranch = "" // Via branch for consultation INVITE (for CANCEL)
    private var consultAuthAttempted = false
    private var consultRtpPort = 0
    private var consultRtpSocket: DatagramSocket? = null
    private var consultRtpSession: RtpSession? = null
    private var consultRemoteRtpHost = ""
    private var consultRemoteRtpPort = 0
    private var consultNegotiatedCodec = 0

    // Consultation state flows
    private val _isConsulting = MutableStateFlow(false)
    val isConsulting: StateFlow<Boolean> = _isConsulting
    private val _consultState = MutableStateFlow(CallState.IDLE)
    val consultState: StateFlow<CallState> = _consultState
    private val _consultNumber = MutableStateFlow("")
    val consultNumber: StateFlow<String> = _consultNumber

    // BLF subscription tracking (Call-ID -> subscription info)
    private data class BlfSubscription(
        val extension: String,
        val callId: String,
        var fromTag: String,
        var cseq: Int = 1,
        var authAttempted: Boolean = false
    )
    private val blfSubscriptions = mutableMapOf<String, BlfSubscription>() // callId -> sub
    private val rejectedBlfCallIds = mutableSetOf<String>() // stale Call-IDs we've already 481'd

    // Outbound MESSAGE tracking (Call-ID -> message info for auth retry)
    private data class PendingMessage(
        val recipient: String,
        val text: String,
        val callId: String,
        val fromTag: String,
        var cseq: Int = 1,
        var authAttempted: Boolean = false
    )
    private val pendingMessages = mutableMapOf<String, PendingMessage>() // callId -> message

    // Jobs
    private var receiverJob: Job? = null
    private var keepaliveJob: Job? = null
    private var reRegisterJob: Job? = null

    // Keepalive health tracking — if server stops responding, re-register
    @Volatile
    private var pendingKeepalives: Int = 0
    private var lastKeepaliveCallId: String = ""

    // Callbacks
    var onCallStateChanged: ((CallState, String, String) -> Unit)? = null
    var onRegistrationChanged: ((RegistrationState) -> Unit)? = null
    var onMessageReceived: ((from: String, body: String) -> Unit)? = null

    fun configure(sipConfig: SipConfig) {
        config = sipConfig
    }

    fun start() {
        if (!config.isValid) {
            Log.w(TAG, "Invalid SIP config, cannot start")
            return
        }
        scope.launch(Dispatchers.IO) {
            try {
                socket?.close()
                socket = DatagramSocket(null).apply {
                    reuseAddress = true
                    bind(java.net.InetSocketAddress(config.localPort))
                    soTimeout = 5000 // 5s timeout so receiver loop can check isActive
                }

                localIp = getLocalIpAddress()
                Log.i(TAG, "SIP started on $localIp:${config.localPort}")

                // STUN discovery for NAT
                discoverPublicAddress()

                // Start receiver loop
                startReceiver()

                // Register with server
                register()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start SIP", e)
                _registrationState.value = RegistrationState.FAILED
                onRegistrationChanged?.invoke(RegistrationState.FAILED)
            }
        }
    }

    @Volatile
    private var stopping = false

    fun stop() {
        scope.launch(Dispatchers.IO) {
            stopBlocking()
        }
    }

    /**
     * Force-restart for network change — closes the old socket immediately
     * without trying to unregister (the old network is dead, so unregister
     * would hang or fail). Then starts fresh on the new network.
     */
    @Volatile
    private var restarting = false

    fun restartForNetworkChange() {
        if (restarting) {
            Log.w(TAG, "Already restarting for network change — skipping")
            return
        }
        restarting = true

        scope.launch(Dispatchers.IO) {
            try {
                Log.i(TAG, "Network change detected — force-restarting SIP stack")

                // Cancel all background jobs
                receiverJob?.cancel()
                keepaliveJob?.cancel()
                reRegisterJob?.cancel()

                // Force-close the old socket (kills receiver loop immediately)
                try { socket?.close() } catch (_: Exception) {}
                socket = null

                // Clean up RTP if active (call will drop on network change anyway)
                rtpSession?.stop()
                rtpSession = null
                try { rtpSocket?.close() } catch (_: Exception) {}
                rtpSocket = null
                consultRtpSession?.stop()
                consultRtpSession = null
                try { consultRtpSocket?.close() } catch (_: Exception) {}
                consultRtpSocket = null

                // Reset registration and auth state for clean re-registration
                _registrationState.value = RegistrationState.UNREGISTERED
                onRegistrationChanged?.invoke(RegistrationState.UNREGISTERED)
                publicIp = ""
                publicPort = 0
                authNonce = ""
                authRealm = ""
                authOpaque = ""
                authQop = ""
                authNonceCount = 0
                authCNonce = ""
                blfSubscriptions.clear()
                rejectedBlfCallIds.clear()
                pendingMessages.clear()
                stopping = false

                // If there was an active call, signal disconnected
                if (_callState.value != CallState.IDLE && _callState.value != CallState.DISCONNECTED) {
                    Log.w(TAG, "Active call dropped due to network change")
                    val num = _remoteNumber.value
                    val name = _remoteName.value
                    resetCallState()
                    _callState.value = CallState.DISCONNECTED
                    onCallStateChanged?.invoke(CallState.DISCONNECTED, num, name)
                }

                // Start fresh on new network
                if (config.isValid) {
                    Log.i(TAG, "Re-starting SIP on new network")
                    try {
                        socket = DatagramSocket(null).apply {
                            reuseAddress = true
                            bind(java.net.InetSocketAddress(config.localPort))
                            soTimeout = 5000
                        }

                        localIp = getLocalIpAddress()
                        Log.i(TAG, "New network IP: $localIp")

                        discoverPublicAddress()
                        startReceiver()
                        // Refresh existing registration so FreeSWITCH replaces the old contact
                        // (instead of adding a new one alongside the dead one)
                        register(refreshExisting = true)
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to restart SIP on new network", e)
                        _registrationState.value = RegistrationState.FAILED
                        onRegistrationChanged?.invoke(RegistrationState.FAILED)
                    }
                }
            } finally {
                restarting = false
            }
        }
    }

    /**
     * Synchronous stop — sends unregister and cleans up.
     * Must be called from a background thread (does network I/O).
     * Used by SipService.onDestroy/onTaskRemoved where async launch may not complete.
     */
    @Synchronized
    fun stopBlocking() {
        if (stopping) return  // Prevent double calls
        stopping = true

        try {
            if (_registrationState.value == RegistrationState.REGISTERED) {
                // Stop background jobs and receiver loop
                keepaliveJob?.cancel()
                reRegisterJob?.cancel()
                receiverJob?.cancel()
                // Socket already has 5s timeout, receiver will exit on next timeout check
                Thread.sleep(200) // Brief pause to let receiver loop see cancellation

                // Set shorter timeout for our unregister reads
                try {
                    socket?.soTimeout = 2000
                } catch (_: Exception) { }

                // Send unregister and handle auth challenge
                unregister()
                Log.i(TAG, "Unregister sent to server")

                // Wait for response — server may challenge with 401
                val sock = socket
                if (sock != null && !sock.isClosed) {
                    try {
                        sock.soTimeout = 2000 // 2 second timeout
                        val buf = ByteArray(4096)
                        val packet = java.net.DatagramPacket(buf, buf.size)
                        sock.receive(packet)
                        val response = String(buf, 0, packet.length)
                        Log.d(TAG, "Unregister response: ${response.take(200)}")

                        if (response.contains("401") || response.contains("407")) {
                            // Parse new auth challenge from full response
                            parseAuthChallenge(response)
                            // Re-send with fresh credentials
                            unregister()
                            Log.i(TAG, "Unregister re-sent with fresh auth")

                            // Wait briefly for 200 OK
                            try {
                                val buf2 = ByteArray(4096)
                                val pkt2 = java.net.DatagramPacket(buf2, buf2.size)
                                sock.receive(pkt2)
                                val resp2 = String(buf2, 0, pkt2.length)
                                Log.d(TAG, "Unregister final response: ${resp2.take(200)}")
                            } catch (_: Exception) { }
                        } else if (response.contains("200")) {
                            Log.i(TAG, "Unregister accepted by server")
                        }
                    } catch (e: java.net.SocketTimeoutException) {
                        Log.w(TAG, "No response to unregister (timeout)")
                    } catch (e: Exception) {
                        Log.w(TAG, "Error reading unregister response: ${e.message}")
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error during unregister", e)
        } finally {
            receiverJob?.cancel()
            keepaliveJob?.cancel()
            reRegisterJob?.cancel()
            rtpSession?.stop()
            rtpSocket?.close()
            rtpSocket = null
            socket?.close()
            socket = null
            blfSubscriptions.clear()
            pendingMessages.clear()
            // Clear Call-ID on full stop so next start gets a fresh registration dialog
            registerCallId = ""
            registerFromTag = ""
            registerCseq = 1
            _registrationState.value = RegistrationState.UNREGISTERED
            onRegistrationChanged?.invoke(RegistrationState.UNREGISTERED)
            stopping = false
        }
    }

    // ==================== REGISTRATION ====================

    private fun register(refreshExisting: Boolean = false) {
        if (refreshExisting && registerCallId.isNotEmpty()) {
            // Re-register on existing dialog (after network change) — FreeSWITCH will
            // replace the old contact instead of adding a new one alongside it
            registerCseq++
            Log.i(TAG, "Refreshing existing registration (Call-ID=$registerCallId, CSeq=$registerCseq)")
        } else {
            registerCallId = generateCallId()
            registerFromTag = generateTag()
            registerCseq = 1
        }
        val contactAddr = contactAddress()

        val request = buildRequest(
            method = "REGISTER",
            requestUri = "sip:${config.domain}",
            toUri = "sip:${config.username}@${config.domain}",
            fromUri = "sip:${config.username}@${config.domain}",
            callId = registerCallId,
            cseq = registerCseq,
            fromTag = registerFromTag,
            extraHeaders = mapOf(
                "Contact" to "<sip:${config.username}@$contactAddr;transport=${config.transport.lowercase()}>",
                "Expires" to REGISTER_EXPIRES.toString()
            )
        )
        _registrationState.value = RegistrationState.REGISTERING
        onRegistrationChanged?.invoke(RegistrationState.REGISTERING)
        sendSip(request)
    }

    /**
     * Fire-and-forget unregister — sends the packet immediately without waiting for response.
     * Used when the process is about to be killed (swipe-away) and there's no time for stopBlocking().
     */
    fun sendQuickUnregister() {
        try {
            if (_registrationState.value == RegistrationState.REGISTERED && registerCallId.isNotEmpty()) {
                Log.i(TAG, "Quick unregister — fire and forget")
                unregister()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Quick unregister failed", e)
        }
    }

    private fun unregister() {
        registerCseq++
        val contactAddr = contactAddress()
        val authHeader = buildAuthHeader("REGISTER", "sip:${config.domain}")
        val headers = mutableMapOf(
            "Contact" to "<sip:${config.username}@$contactAddr;transport=${config.transport.lowercase()}>",
            "Expires" to "0"
        )
        if (authHeader.isNotEmpty()) {
            headers["Authorization"] = authHeader
        }
        val request = buildRequest(
            method = "REGISTER",
            requestUri = "sip:${config.domain}",
            toUri = "sip:${config.username}@${config.domain}",
            fromUri = "sip:${config.username}@${config.domain}",
            callId = registerCallId,
            cseq = registerCseq,
            fromTag = registerFromTag,
            extraHeaders = headers
        )
        sendSip(request)
    }

    private fun registerWithAuth(isProxy: Boolean) {
        registerCseq++
        val contactAddr = contactAddress()
        val authHeader = buildAuthHeader("REGISTER", "sip:${config.domain}")
        val headerName = if (isProxy) "Proxy-Authorization" else "Authorization"

        val request = buildRequest(
            method = "REGISTER",
            requestUri = "sip:${config.domain}",
            toUri = "sip:${config.username}@${config.domain}",
            fromUri = "sip:${config.username}@${config.domain}",
            callId = registerCallId,
            cseq = registerCseq,
            fromTag = registerFromTag,
            extraHeaders = mapOf(
                "Contact" to "<sip:${config.username}@$contactAddr;transport=${config.transport.lowercase()}>",
                "Expires" to REGISTER_EXPIRES.toString(),
                headerName to authHeader
            )
        )
        sendSip(request)
    }

    // ==================== CALL MANAGEMENT ====================

    fun sendMessage(recipient: String, text: String) {
        if (_registrationState.value != RegistrationState.REGISTERED) {
            Log.w(TAG, "Cannot send message — not registered")
            return
        }
        scope.launch(Dispatchers.IO) {
            val msgCallId = generateCallId()
            val fromTag = generateTag()
            val contactAddr = contactAddress()
            val pending = PendingMessage(
                recipient = recipient,
                text = text,
                callId = msgCallId,
                fromTag = fromTag
            )
            pendingMessages[msgCallId] = pending
            val request = buildRequest(
                method = "MESSAGE",
                requestUri = "sip:$recipient@${config.domain}",
                toUri = "sip:$recipient@${config.domain}",
                fromUri = "sip:${config.username}@${config.domain}",
                callId = msgCallId,
                cseq = 1,
                fromTag = fromTag,
                extraHeaders = mapOf(
                    "Contact" to "<sip:${config.username}@$contactAddr>",
                    "Content-Type" to "text/plain"
                ),
                body = text
            )
            sendSip(request)
            Log.i(TAG, "Sent MESSAGE to $recipient: ${text.take(50)}")
        }
    }

    fun makeCall(number: String) {
        Log.i(TAG, "makeCall requested: $number, current state: ${_callState.value}, registration: ${_registrationState.value}")
        if (_callState.value != CallState.IDLE) {
            Log.w(TAG, "Cannot make call, state is ${_callState.value}")
            return
        }
        if (_registrationState.value != RegistrationState.REGISTERED) {
            Log.w(TAG, "Cannot make call — not registered (state: ${_registrationState.value})")
            return
        }

        scope.launch(Dispatchers.IO) {
            currentCallId = generateCallId()
            currentLocalTag = generateTag()
            currentRemoteTag = ""
            currentCallDirection = "outbound"
            currentCSeq = 1
            inviteAuthAttempted = false
            pendingHoldMode = false
            currentRemoteUri = "sip:$number@${config.domain}"

            _remoteNumber.value = number
            _remoteName.value = ""
            _callState.value = CallState.CALLING
            onCallStateChanged?.invoke(CallState.CALLING, number, "")

            // Allocate a verified RTP port
            localRtpPort = allocateRtpPort()
            val sdp = buildSdp(localRtpPort)
            val contactAddr = contactAddress()

            val headers = mutableMapOf(
                "Contact" to "<sip:${config.username}@$contactAddr>",
                "Content-Type" to "application/sdp",
                "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
            )

            // Don't send cached REGISTER auth for INVITEs - the proxy uses a different
            // nonce/challenge. Let the 407 flow handle INVITE authentication properly.

            currentInviteBranch = "z9hG4bK${Random.nextInt(100000, 999999)}"
            val request = buildRequest(
                method = "INVITE",
                requestUri = currentRemoteUri,
                toUri = currentRemoteUri,
                fromUri = "sip:${config.username}@${config.domain}",
                callId = currentCallId,
                cseq = currentCSeq,
                fromTag = currentLocalTag,
                extraHeaders = headers,
                body = sdp,
                viaBranch = currentInviteBranch
            )
            sendSip(request)
        }
    }

    fun answerCall() {
        if (_callState.value != CallState.INCOMING) return

        scope.launch(Dispatchers.IO) {
            if (localRtpPort == 0) localRtpPort = allocateRtpPort()
            val sdp = buildSdp(localRtpPort)
            val contactAddr = contactAddress()

            // Mirror the incoming INVITE's Via, From, To, CSeq headers
            val invite = incomingInviteMsg ?: return@launch
            val viaHeader = extractHeader(invite, "Via") ?: ""
            val fromHeader = extractHeader(invite, "From") ?: ""
            val toHeader = extractHeader(invite, "To") ?: ""
            val cseqHeader = extractHeader(invite, "CSeq") ?: ""

            // Add our tag to To header
            val toWithTag = if (!toHeader.contains("tag="))
                "$toHeader;tag=$currentLocalTag" else toHeader

            val response = "SIP/2.0 200 OK\r\n" +
                "Via: $viaHeader\r\n" +
                "From: $fromHeader\r\n" +
                "To: $toWithTag\r\n" +
                "Call-ID: $currentCallId\r\n" +
                "CSeq: $cseqHeader\r\n" +
                "Contact: <sip:${config.username}@$contactAddr>\r\n" +
                "Content-Type: application/sdp\r\n" +
                "User-Agent: MyLineTelecom-Android/1.0\r\n" +
                "Content-Length: ${sdp.toByteArray().size}\r\n\r\n" +
                sdp

            sendSip(response)
            startRtp()
            _callState.value = CallState.CONFIRMED
            onCallStateChanged?.invoke(CallState.CONFIRMED, _remoteNumber.value, _remoteName.value)
        }
    }

    fun hangup() {
        val state = _callState.value
        if (state == CallState.IDLE || state == CallState.DISCONNECTED) return

        scope.launch(Dispatchers.IO) {
            when (state) {
                CallState.CALLING, CallState.RINGING -> {
                    // Send CANCEL - must use same Via branch as the INVITE being cancelled
                    val request = buildRequest(
                        method = "CANCEL",
                        requestUri = currentRemoteUri,
                        toUri = currentRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = currentCallId,
                        cseq = currentCSeq,
                        fromTag = currentLocalTag,
                        toTag = currentRemoteTag,
                        viaBranch = currentInviteBranch
                    )
                    sendSip(request)
                }
                CallState.INCOMING -> {
                    // Reject with 486 Busy
                    val invite = incomingInviteMsg ?: return@launch
                    val response = buildMirroredResponse(486, "Busy Here", invite, currentLocalTag)
                    sendSip(response)
                }
                else -> {
                    // Send BYE
                    currentCSeq++
                    val request = buildRequest(
                        method = "BYE",
                        requestUri = currentRemoteUri,
                        toUri = currentRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = currentCallId,
                        cseq = currentCSeq,
                        fromTag = currentLocalTag,
                        toTag = currentRemoteTag
                    )
                    sendSip(request)
                }
            }

            rtpSession?.stop()
            rtpSession = null

            // If consultation call is active, BYE it too
            if (_isConsulting.value && consultCallId.isNotEmpty()) {
                if (_consultState.value == CallState.CONFIRMED) {
                    consultCSeq++
                    val consultBye = buildRequest(
                        method = "BYE",
                        requestUri = consultRemoteUri,
                        toUri = consultRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = consultCallId,
                        cseq = consultCSeq,
                        fromTag = consultLocalTag,
                        toTag = consultRemoteTag
                    )
                    sendSip(consultBye)
                } else if (_consultState.value == CallState.CALLING || _consultState.value == CallState.RINGING) {
                    val consultCancel = buildRequest(
                        method = "CANCEL",
                        requestUri = consultRemoteUri,
                        toUri = consultRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = consultCallId,
                        cseq = consultCSeq,
                        fromTag = consultLocalTag,
                        toTag = consultRemoteTag,
                        viaBranch = consultInviteBranch
                    )
                    sendSip(consultCancel)
                }
            }

            _callState.value = CallState.DISCONNECTED
            onCallStateChanged?.invoke(CallState.DISCONNECTED, _remoteNumber.value, _remoteName.value)
            delay(1000)
            resetCallState()
        }
    }

    fun toggleMute(): Boolean {
        val rtp = rtpSession ?: return false
        val newMuted = !rtp.isMuted
        rtp.mute(newMuted)
        return newMuted
    }

    fun toggleHold() {
        scope.launch(Dispatchers.IO) {
            if (_callState.value == CallState.CONFIRMED) {
                // Put on hold
                pendingHoldMode = true
                inviteAuthAttempted = false
                currentCSeq++
                val sdp = buildSdp(localRtpPort, holdMode = true)
                val request = buildRequest(
                    method = "INVITE",
                    requestUri = currentRemoteUri,
                    toUri = currentRemoteUri,
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = currentCallId,
                    cseq = currentCSeq,
                    fromTag = currentLocalTag,
                    toTag = currentRemoteTag,
                    extraHeaders = mapOf(
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        "Content-Type" to "application/sdp",
                        "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
                    ),
                    body = sdp
                )
                sendSip(request)
                rtpSession?.mute(true)
                _callState.value = CallState.HOLD
                onCallStateChanged?.invoke(CallState.HOLD, _remoteNumber.value, _remoteName.value)
            } else if (_callState.value == CallState.HOLD) {
                // Unhold
                pendingHoldMode = false
                inviteAuthAttempted = false
                currentCSeq++
                val sdp = buildSdp(localRtpPort)
                val request = buildRequest(
                    method = "INVITE",
                    requestUri = currentRemoteUri,
                    toUri = currentRemoteUri,
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = currentCallId,
                    cseq = currentCSeq,
                    fromTag = currentLocalTag,
                    toTag = currentRemoteTag,
                    extraHeaders = mapOf(
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        "Content-Type" to "application/sdp",
                        "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
                    ),
                    body = sdp
                )
                sendSip(request)
                rtpSession?.mute(false)
                _callState.value = CallState.CONFIRMED
                onCallStateChanged?.invoke(CallState.CONFIRMED, _remoteNumber.value, _remoteName.value)
            }
        }
    }

    fun sendDtmf(digit: Char) {
        rtpSession?.sendDtmf(digit)
    }

    fun blindTransfer(target: String) {
        if (_callState.value != CallState.CONFIRMED && _callState.value != CallState.HOLD) return
        scope.launch(Dispatchers.IO) {
            pendingReferTarget = target
            referAuthAttempted = false
            currentCSeq++
            sendRefer(target)
        }
    }

    private fun sendRefer(target: String, authHeader: Pair<String, String>? = null) {
        val referTo = "sip:$target@${config.domain}"
        val headers = mutableMapOf(
            "Refer-To" to "<$referTo>",
            "Referred-By" to "<sip:${config.username}@${config.domain}>",
            "Contact" to "<sip:${config.username}@${contactAddress()}>",
            "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
        )
        if (authHeader != null) {
            headers[authHeader.first] = authHeader.second
        }

        val request = buildRequest(
            method = "REFER",
            requestUri = currentRemoteUri,
            toUri = currentRemoteUri,
            fromUri = "sip:${config.username}@${config.domain}",
            callId = currentCallId,
            cseq = currentCSeq,
            fromTag = currentLocalTag,
            toTag = currentRemoteTag,
            extraHeaders = headers
        )
        sendSip(request)
    }

    fun attendedTransferStart(target: String) {
        if (_callState.value != CallState.CONFIRMED && _callState.value != CallState.HOLD) return

        scope.launch(Dispatchers.IO) {
            // Step 1: Put the original call on hold (if not already)
            if (_callState.value == CallState.CONFIRMED) {
                pendingHoldMode = true
                inviteAuthAttempted = false
                currentCSeq++
                val sdp = buildSdp(localRtpPort, holdMode = true)
                val request = buildRequest(
                    method = "INVITE",
                    requestUri = currentRemoteUri,
                    toUri = currentRemoteUri,
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = currentCallId,
                    cseq = currentCSeq,
                    fromTag = currentLocalTag,
                    toTag = currentRemoteTag,
                    extraHeaders = mapOf(
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        "Content-Type" to "application/sdp",
                        "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
                    ),
                    body = sdp
                )
                sendSip(request)
                rtpSession?.mute(true)
                _callState.value = CallState.HOLD
                onCallStateChanged?.invoke(CallState.HOLD, _remoteNumber.value, _remoteName.value)

                // Wait a bit for the hold to be acknowledged
                delay(500)
            }

            // Step 2: Start consultation call to target
            consultCallId = generateCallId()
            consultLocalTag = generateTag()
            consultRemoteTag = ""
            consultRemoteUri = "sip:$target@${config.domain}"
            consultCSeq = 1
            consultAuthAttempted = false

            _consultNumber.value = target
            _consultState.value = CallState.CALLING
            _isConsulting.value = true

            // Allocate RTP port for consultation call
            consultRtpSocket?.close()
            consultRtpSocket = null
            for (port in 10002..20000 step 2) {
                if (port == localRtpPort) continue // Don't reuse the held call's port
                try {
                    consultRtpSocket = DatagramSocket(port)
                    consultRtpPort = port
                    break
                } catch (_: Exception) { }
            }
            if (consultRtpSocket == null) {
                consultRtpSocket = DatagramSocket()
                consultRtpPort = consultRtpSocket!!.localPort
            }

            val sdp = buildSdp(consultRtpPort)
            val contactAddr = contactAddress()

            consultInviteBranch = "z9hG4bK${Random.nextInt(100000, 999999)}"
            val request = buildRequest(
                method = "INVITE",
                requestUri = consultRemoteUri,
                toUri = consultRemoteUri,
                fromUri = "sip:${config.username}@${config.domain}",
                callId = consultCallId,
                cseq = consultCSeq,
                fromTag = consultLocalTag,
                extraHeaders = mapOf(
                    "Contact" to "<sip:${config.username}@$contactAddr>",
                    "Content-Type" to "application/sdp",
                    "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
                ),
                body = sdp,
                viaBranch = consultInviteBranch
            )
            sendSip(request)
            Log.i(TAG, "Consultation call started to $target (Call-ID: $consultCallId)")
        }
    }

    fun attendedTransferComplete() {
        if (!_isConsulting.value || _consultState.value != CallState.CONFIRMED) return

        scope.launch(Dispatchers.IO) {
            // Send REFER on the original call (Call A) with Replaces pointing to consultation call (Call B)
            // This tells the original caller to connect to the consultation target
            // URL-encode the Replaces header value for inclusion in the Refer-To URI
            // Replaces: call-id;to-tag=X;from-tag=Y → encoded as URI parameter
            val encodedCallId = consultCallId.replace("@", "%40")
            val replacesValue = "$encodedCallId%3Bto-tag%3D$consultRemoteTag%3Bfrom-tag%3D$consultLocalTag"

            val referTo = "<sip:${_consultNumber.value}@${config.domain}?Replaces=$replacesValue>"

            pendingReferTarget = _consultNumber.value
            referAuthAttempted = false
            currentCSeq++

            val headers = mutableMapOf(
                "Refer-To" to referTo,
                "Referred-By" to "<sip:${config.username}@${config.domain}>",
                "Contact" to "<sip:${config.username}@${contactAddress()}>",
                "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
            )

            val request = buildRequest(
                method = "REFER",
                requestUri = currentRemoteUri,
                toUri = currentRemoteUri,
                fromUri = "sip:${config.username}@${config.domain}",
                callId = currentCallId,
                cseq = currentCSeq,
                fromTag = currentLocalTag,
                toTag = currentRemoteTag,
                extraHeaders = headers
            )
            sendSip(request)
            Log.i(TAG, "Attended transfer REFER sent with Replaces")
        }
    }

    fun attendedTransferCancel() {
        if (!_isConsulting.value) return

        scope.launch(Dispatchers.IO) {
            // Hang up the consultation call
            when (_consultState.value) {
                CallState.CALLING, CallState.RINGING -> {
                    // CANCEL the consultation INVITE (must use same Via branch)
                    val request = buildRequest(
                        method = "CANCEL",
                        requestUri = consultRemoteUri,
                        toUri = consultRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = consultCallId,
                        cseq = consultCSeq,
                        fromTag = consultLocalTag,
                        toTag = consultRemoteTag,
                        viaBranch = consultInviteBranch
                    )
                    sendSip(request)
                }
                CallState.CONFIRMED -> {
                    // BYE the consultation call
                    consultCSeq++
                    val request = buildRequest(
                        method = "BYE",
                        requestUri = consultRemoteUri,
                        toUri = consultRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = consultCallId,
                        cseq = consultCSeq,
                        fromTag = consultLocalTag,
                        toTag = consultRemoteTag
                    )
                    sendSip(request)
                }
                else -> {}
            }

            // Clean up consultation call
            cleanupConsultation()

            // Resume the original call (unhold)
            delay(300)
            pendingHoldMode = false
            inviteAuthAttempted = false
            currentCSeq++
            val sdp = buildSdp(localRtpPort)
            val request = buildRequest(
                method = "INVITE",
                requestUri = currentRemoteUri,
                toUri = currentRemoteUri,
                fromUri = "sip:${config.username}@${config.domain}",
                callId = currentCallId,
                cseq = currentCSeq,
                fromTag = currentLocalTag,
                toTag = currentRemoteTag,
                extraHeaders = mapOf(
                    "Contact" to "<sip:${config.username}@${contactAddress()}>",
                    "Content-Type" to "application/sdp",
                    "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"
                ),
                body = sdp
            )
            sendSip(request)
            rtpSession?.mute(false)
            _callState.value = CallState.CONFIRMED
            onCallStateChanged?.invoke(CallState.CONFIRMED, _remoteNumber.value, _remoteName.value)
            Log.i(TAG, "Attended transfer cancelled, resuming original call")
        }
    }

    private fun cleanupConsultation() {
        consultRtpSession?.stop()
        consultRtpSession = null
        consultRtpSocket?.close()
        consultRtpSocket = null
        consultCallId = ""
        consultLocalTag = ""
        consultRemoteTag = ""
        consultRemoteUri = ""
        consultCSeq = 1
        consultInviteBranch = ""
        consultAuthAttempted = false
        consultRtpPort = 0
        consultRemoteRtpHost = ""
        consultRemoteRtpPort = 0
        consultNegotiatedCodec = 0
        _isConsulting.value = false
        _consultState.value = CallState.IDLE
        _consultNumber.value = ""
    }

    fun subscribeBlfExtension(extension: String) {
        scope.launch(Dispatchers.IO) {
            val subCallId = generateCallId()
            val fromTag = generateTag()
            val sub = BlfSubscription(
                extension = extension,
                callId = subCallId,
                fromTag = fromTag,
                cseq = 1,
                authAttempted = false
            )
            blfSubscriptions[subCallId] = sub
            Log.i(TAG, "Subscribing BLF for $extension (Call-ID: $subCallId)")

            val request = buildRequest(
                method = "SUBSCRIBE",
                requestUri = "sip:$extension@${config.domain}",
                toUri = "sip:$extension@${config.domain}",
                fromUri = "sip:${config.username}@${config.domain}",
                callId = subCallId,
                cseq = 1,
                fromTag = fromTag,
                extraHeaders = mapOf(
                    "Event" to "dialog",
                    "Accept" to "application/dialog-info+xml",
                    "Expires" to "3600",
                    "Contact" to "<sip:${config.username}@${contactAddress()}>"
                )
            )
            sendSip(request)
        }
    }

    fun unsubscribeBlfExtension(extension: String) {
        scope.launch(Dispatchers.IO) {
            val subCallId = generateCallId()
            val fromTag = generateTag()
            val request = buildRequest(
                method = "SUBSCRIBE",
                requestUri = "sip:$extension@${config.domain}",
                toUri = "sip:$extension@${config.domain}",
                fromUri = "sip:${config.username}@${config.domain}",
                callId = subCallId,
                cseq = 1,
                fromTag = fromTag,
                extraHeaders = mapOf(
                    "Event" to "dialog",
                    "Accept" to "application/dialog-info+xml",
                    "Expires" to "0",
                    "Contact" to "<sip:${config.username}@${contactAddress()}>"
                )
            )
            sendSip(request)
        }
    }

    // ==================== MESSAGE RECEIVER ====================

    private fun startReceiver() {
        receiverJob?.cancel()
        receiverJob = scope.launch(Dispatchers.IO) {
            val buffer = ByteArray(8192)
            while (isActive) {
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket?.receive(packet)
                    val message = String(packet.data, 0, packet.length)
                    if (message.isBlank()) continue

                    Log.d(TAG, "<<< Received:\n${message.take(600)}")

                    // Any received packet proves the server can reach us — clear keepalive failure counter
                    pendingKeepalives = 0

                    if (message.startsWith("SIP/2.0")) {
                        handleResponse(message)
                    } else {
                        handleRequest(message)
                    }
                } catch (_: java.net.SocketTimeoutException) {
                    // Normal — socket has 5s timeout so we can check isActive periodically
                    continue
                } catch (e: Exception) {
                    if (isActive) Log.e(TAG, "Receiver error", e)
                }
            }
        }
    }

    // ==================== RESPONSE HANDLING ====================

    private fun handleResponse(message: String) {
        val statusLine = message.lines().firstOrNull() ?: return
        val statusCode = statusLine.split(" ").getOrNull(1)?.toIntOrNull() ?: return
        val cseqLine = extractHeader(message, "CSeq") ?: return
        val method = cseqLine.split(" ").lastOrNull() ?: return
        val msgCallId = extractHeader(message, "Call-ID") ?: ""

        when {
            method == "REGISTER" && msgCallId == registerCallId -> handleRegisterResponse(statusCode, message)
            method == "INVITE" && msgCallId == consultCallId && consultCallId.isNotEmpty() -> handleConsultInviteResponse(statusCode, message)
            method == "INVITE" && msgCallId == currentCallId -> handleInviteResponse(statusCode, message)
            method == "BYE" -> {
                if (statusCode == 200) {
                    Log.i(TAG, "BYE acknowledged")
                }
            }
            method == "REFER" && msgCallId == currentCallId -> {
                handleReferResponse(statusCode, message)
            }
            method == "SUBSCRIBE" && blfSubscriptions.containsKey(msgCallId) -> {
                handleSubscribeResponse(statusCode, message, msgCallId)
            }
            method == "OPTIONS" && statusCode == 200 -> {
                // Server is alive — clear pending keepalive counter
                pendingKeepalives = 0
                // Check if our public IP/port changed (NAT rebind detection)
                checkNatChanged(message)
            }
            method == "MESSAGE" -> handleMessageResponse(statusCode, message, msgCallId)
        }
    }

    private fun handleMessageResponse(statusCode: Int, message: String, callId: String) {
        val pending = pendingMessages[callId] ?: return
        when (statusCode) {
            200, 202 -> {
                Log.i(TAG, "MESSAGE delivered to ${pending.recipient}")
                pendingMessages.remove(callId)
            }
            401, 407 -> {
                if (pending.authAttempted) {
                    Log.w(TAG, "MESSAGE auth failed for ${pending.recipient}")
                    pendingMessages.remove(callId)
                    return
                }
                pending.authAttempted = true
                parseAuthChallenge(message)
                pending.cseq++
                val toUri = "sip:${pending.recipient}@${config.domain}"
                val authHeader = buildAuthHeader("MESSAGE", toUri)
                val headerName = if (statusCode == 407) "Proxy-Authorization" else "Authorization"
                val request = buildRequest(
                    method = "MESSAGE",
                    requestUri = toUri,
                    toUri = toUri,
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = callId,
                    cseq = pending.cseq,
                    fromTag = pending.fromTag,
                    extraHeaders = mapOf(
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        "Content-Type" to "text/plain",
                        headerName to authHeader
                    ),
                    body = pending.text
                )
                sendSip(request)
                Log.i(TAG, "MESSAGE auth retry to ${pending.recipient}")
            }
            else -> {
                Log.w(TAG, "MESSAGE to ${pending.recipient} failed: $statusCode")
                pendingMessages.remove(callId)
            }
        }
    }

    private fun handleSubscribeResponse(statusCode: Int, message: String, callId: String) {
        val sub = blfSubscriptions[callId] ?: return

        when (statusCode) {
            200, 202 -> {
                Log.i(TAG, "BLF subscription accepted for ${sub.extension}")
                sub.authAttempted = false
            }
            401, 407 -> {
                if (sub.authAttempted) {
                    Log.w(TAG, "BLF SUBSCRIBE auth failed for ${sub.extension}")
                    blfSubscriptions.remove(callId)
                    return
                }
                sub.authAttempted = true

                // Parse auth challenge from this response
                parseAuthChallenge(message)

                sub.cseq++
                val authHeader = buildAuthHeader(
                    method = "SUBSCRIBE",
                    uri = "sip:${sub.extension}@${config.domain}"
                )

                val headerName = if (statusCode == 407) "Proxy-Authorization" else "Authorization"

                val request = buildRequest(
                    method = "SUBSCRIBE",
                    requestUri = "sip:${sub.extension}@${config.domain}",
                    toUri = "sip:${sub.extension}@${config.domain}",
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = callId,
                    cseq = sub.cseq,
                    fromTag = sub.fromTag,
                    extraHeaders = mapOf(
                        "Event" to "dialog",
                        "Accept" to "application/dialog-info+xml",
                        "Expires" to "3600",
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        headerName to authHeader
                    )
                )
                sendSip(request)
                Log.i(TAG, "BLF SUBSCRIBE auth retry for ${sub.extension}")
            }
            else -> {
                Log.w(TAG, "BLF SUBSCRIBE failed for ${sub.extension}: $statusCode")
                blfSubscriptions.remove(callId)
            }
        }
    }

    private fun handleRegisterResponse(statusCode: Int, message: String) {
        when (statusCode) {
            200 -> {
                // Check if this is a response to an unregister (Expires: 0)
                if (stopping) {
                    Log.i(TAG, "Unregister 200 OK received (handled by receiver loop)")
                    return
                }

                // If STUN failed, learn our public address from the server's Via response
                val hadPublicAddr = publicIp.isNotEmpty() && publicPort > 0
                learnPublicAddressFromResponse(message)

                // If we just learned our public address (STUN had failed), re-register
                // with the correct Contact so the server can reach us for incoming calls
                if (!hadPublicAddr && publicIp.isNotEmpty() && publicPort > 0) {
                    Log.i(TAG, "Re-registering with correct public address: $publicIp:$publicPort")
                    registerCseq++
                    val contactAddr = contactAddress()
                    val authHeader = buildAuthHeader("REGISTER", "sip:${config.domain}")
                    val headers = mutableMapOf(
                        "Contact" to "<sip:${config.username}@$contactAddr;transport=${config.transport.lowercase()}>",
                        "Expires" to REGISTER_EXPIRES.toString()
                    )
                    if (authHeader.isNotEmpty()) {
                        headers["Authorization"] = authHeader
                    }
                    val request = buildRequest(
                        method = "REGISTER",
                        requestUri = "sip:${config.domain}",
                        toUri = "sip:${config.username}@${config.domain}",
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = registerCallId,
                        cseq = registerCseq,
                        fromTag = registerFromTag,
                        extraHeaders = headers
                    )
                    sendSip(request)
                    return // Wait for the new 200 OK with correct Contact
                }

                Log.i(TAG, "Registered successfully")
                pendingKeepalives = 0
                _registrationState.value = RegistrationState.REGISTERED
                onRegistrationChanged?.invoke(RegistrationState.REGISTERED)
                startKeepalive()
                scheduleReRegister()
            }
            401, 407 -> {
                parseAuthChallenge(message)
                registerWithAuth(statusCode == 407)
            }
            else -> {
                Log.w(TAG, "Registration failed: $statusCode")
                _registrationState.value = RegistrationState.FAILED
                onRegistrationChanged?.invoke(RegistrationState.FAILED)
            }
        }
    }

    private fun handleInviteResponse(statusCode: Int, message: String) {
        // CRITICAL: Check CSeq number to ignore retransmitted responses for old requests.
        // UDP is unreliable, so the server may retransmit a 407 for CSeq 1 even after
        // we've already sent a new INVITE with CSeq 2 and received 183/200 for it.
        val cseqLine = extractHeader(message, "CSeq") ?: return
        val responseCSeq = cseqLine.split(" ").firstOrNull()?.trim()?.toIntOrNull() ?: return
        if (responseCSeq < currentCSeq) {
            Log.d(TAG, "Ignoring retransmitted response for old CSeq $responseCSeq (current: $currentCSeq)")
            return
        }

        when (statusCode) {
            100 -> { /* Trying - ignore */ }
            180, 183 -> {
                if (_callState.value == CallState.CALLING) {
                    currentRemoteTag = extractTagFromHeader(message, "To") ?: currentRemoteTag
                    _callState.value = CallState.RINGING
                    onCallStateChanged?.invoke(CallState.RINGING, _remoteNumber.value, _remoteName.value)
                }
            }
            200 -> {
                currentRemoteTag = extractTagFromHeader(message, "To") ?: currentRemoteTag
                parseSdpFromMessage(message)
                sendAck(message)

                // Reset auth flag so future re-INVITEs (hold/unhold) can authenticate
                inviteAuthAttempted = false

                // Only start RTP for initial INVITE 200 OK, not re-INVITE (hold/unhold)
                if (_callState.value == CallState.CALLING || _callState.value == CallState.RINGING) {
                    startRtp()
                    _callState.value = CallState.CONFIRMED
                    onCallStateChanged?.invoke(CallState.CONFIRMED, _remoteNumber.value, _remoteName.value)
                }
            }
            401, 407 -> {
                if (inviteAuthAttempted) {
                    Log.w(TAG, "INVITE auth failed repeatedly")
                    sendAck(message, isNon2xx = true)
                    // If we're already in a call (re-INVITE for hold/unhold), don't kill the call
                    if (_callState.value == CallState.CALLING || _callState.value == CallState.RINGING) {
                        _callState.value = CallState.REJECTED
                        onCallStateChanged?.invoke(CallState.REJECTED, _remoteNumber.value, _remoteName.value)
                        scope.launch { delay(1500); resetCallState() }
                    } else {
                        Log.w(TAG, "Re-INVITE auth failed, keeping call active")
                    }
                    return
                }
                inviteAuthAttempted = true
                parseAuthChallenge(message)
                sendAck(message, isNon2xx = true)

                // Re-send INVITE with auth
                currentCSeq++
                resendInviteWithAuth(statusCode == 407)
            }
            486, 600 -> {
                sendAck(message, isNon2xx = true)
                _callState.value = CallState.BUSY
                onCallStateChanged?.invoke(CallState.BUSY, _remoteNumber.value, _remoteName.value)
                scope.launch { delay(1500); resetCallState() }
            }
            in 400..699 -> {
                Log.w(TAG, "Call rejected: $statusCode")
                sendAck(message, isNon2xx = true)
                _callState.value = CallState.REJECTED
                onCallStateChanged?.invoke(CallState.REJECTED, _remoteNumber.value, _remoteName.value)
                scope.launch { delay(1500); resetCallState() }
            }
        }
    }

    private fun handleReferResponse(statusCode: Int, message: String) {
        when (statusCode) {
            200, 202 -> {
                Log.i(TAG, "Transfer accepted ($statusCode)")
                // The server will send NOTIFY with transfer progress
                // and BYE when the transfer completes - we'll handle it there
            }
            401, 407 -> {
                if (referAuthAttempted) {
                    Log.w(TAG, "REFER auth failed repeatedly")
                    return
                }
                referAuthAttempted = true
                parseAuthChallenge(message)
                currentCSeq++
                val headerName = if (statusCode == 407) "Proxy-Authorization" else "Authorization"
                val headerValue = buildAuthHeader("REFER", currentRemoteUri)

                if (_isConsulting.value && consultCallId.isNotEmpty()) {
                    // Attended transfer - resend REFER with Replaces
                    val encodedCallId = consultCallId.replace("@", "%40")
                    val replacesValue = "$encodedCallId%3Bto-tag%3D$consultRemoteTag%3Bfrom-tag%3D$consultLocalTag"
                    val referTo = "<sip:${_consultNumber.value}@${config.domain}?Replaces=$replacesValue>"

                    val headers = mutableMapOf(
                        "Refer-To" to referTo,
                        "Referred-By" to "<sip:${config.username}@${config.domain}>",
                        "Contact" to "<sip:${config.username}@${contactAddress()}>",
                        "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER",
                        headerName to headerValue
                    )

                    val request = buildRequest(
                        method = "REFER",
                        requestUri = currentRemoteUri,
                        toUri = currentRemoteUri,
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = currentCallId,
                        cseq = currentCSeq,
                        fromTag = currentLocalTag,
                        toTag = currentRemoteTag,
                        extraHeaders = headers
                    )
                    sendSip(request)
                } else {
                    // Blind transfer - resend simple REFER with auth
                    sendRefer(pendingReferTarget, Pair(headerName, headerValue))
                }
            }
            else -> {
                Log.w(TAG, "Transfer rejected: $statusCode")
            }
        }
    }

    // ==================== CONSULTATION CALL RESPONSE HANDLING ====================

    private fun handleConsultInviteResponse(statusCode: Int, message: String) {
        val cseqLine = extractHeader(message, "CSeq") ?: return
        val responseCSeq = cseqLine.split(" ").firstOrNull()?.trim()?.toIntOrNull() ?: return
        if (responseCSeq < consultCSeq) {
            Log.d(TAG, "Ignoring retransmitted consult response for old CSeq $responseCSeq (current: $consultCSeq)")
            return
        }

        when (statusCode) {
            100 -> { /* Trying */ }
            180, 183 -> {
                if (_consultState.value == CallState.CALLING) {
                    consultRemoteTag = extractTagFromHeader(message, "To") ?: consultRemoteTag
                    _consultState.value = CallState.RINGING
                    Log.i(TAG, "Consultation call ringing")
                }
            }
            200 -> {
                consultRemoteTag = extractTagFromHeader(message, "To") ?: consultRemoteTag
                // Parse SDP for consultation RTP
                parseConsultSdpFromMessage(message)
                // Send ACK for consultation call
                sendConsultAck(message)
                consultAuthAttempted = false

                if (_consultState.value == CallState.CALLING || _consultState.value == CallState.RINGING) {
                    // Start consultation RTP
                    startConsultRtp()
                    _consultState.value = CallState.CONFIRMED
                    Log.i(TAG, "Consultation call connected - can now complete attended transfer")
                }
            }
            401, 407 -> {
                if (consultAuthAttempted) {
                    Log.w(TAG, "Consultation INVITE auth failed repeatedly")
                    sendConsultAck(message, isNon2xx = true)
                    cleanupConsultation()
                    return
                }
                consultAuthAttempted = true
                parseAuthChallenge(message)
                sendConsultAck(message, isNon2xx = true)

                consultCSeq++
                val contactAddr = contactAddress()
                val authHeader = buildAuthHeader("INVITE", consultRemoteUri)
                val headerName = if (statusCode == 407) "Proxy-Authorization" else "Authorization"
                val sdp = buildSdp(consultRtpPort)

                consultInviteBranch = "z9hG4bK${Random.nextInt(100000, 999999)}"
                val request = buildRequest(
                    method = "INVITE",
                    requestUri = consultRemoteUri,
                    toUri = consultRemoteUri,
                    fromUri = "sip:${config.username}@${config.domain}",
                    callId = consultCallId,
                    cseq = consultCSeq,
                    fromTag = consultLocalTag,
                    extraHeaders = mapOf(
                        "Contact" to "<sip:${config.username}@$contactAddr>",
                        "Content-Type" to "application/sdp",
                        "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER",
                        headerName to authHeader
                    ),
                    body = sdp,
                    viaBranch = consultInviteBranch
                )
                sendSip(request)
            }
            486, 600 -> {
                sendConsultAck(message, isNon2xx = true)
                Log.w(TAG, "Consultation call: busy ($statusCode)")
                cleanupConsultation()
            }
            in 400..699 -> {
                Log.w(TAG, "Consultation call rejected: $statusCode")
                sendConsultAck(message, isNon2xx = true)
                cleanupConsultation()
            }
        }
    }

    private fun sendConsultAck(responseMessage: String, isNon2xx: Boolean = false) {
        val toHeader = extractHeader(responseMessage, "To") ?: ""
        val remoteTag = extractTagFromHeaderValue(toHeader) ?: consultRemoteTag

        // RFC 3261: ACK for non-2xx MUST use the same Via branch as the original INVITE
        val branch = if (isNon2xx) extractViaBranch(responseMessage) else ""

        val request = buildRequest(
            method = "ACK",
            requestUri = consultRemoteUri,
            toUri = consultRemoteUri,
            fromUri = "sip:${config.username}@${config.domain}",
            callId = consultCallId,
            cseq = consultCSeq,
            fromTag = consultLocalTag,
            toTag = remoteTag,
            viaBranch = branch
        )
        sendSip(request)
    }

    private fun parseConsultSdpFromMessage(message: String) {
        val body = extractSipBody(message)
        if (body.isBlank()) return
        for (line in body.lines()) {
            when {
                line.startsWith("c=IN IP4 ") -> {
                    consultRemoteRtpHost = line.substringAfter("c=IN IP4 ").trim()
                }
                line.startsWith("m=audio ") -> {
                    val parts = line.split(" ")
                    if (parts.size >= 2) {
                        consultRemoteRtpPort = parts[1].toIntOrNull() ?: 0
                    }
                    if (parts.size >= 4) {
                        for (i in 3 until parts.size) {
                            val pt = parts[i].trim().toIntOrNull() ?: continue
                            if (pt in listOf(0, 3, 8, 18)) {
                                consultNegotiatedCodec = pt
                                break
                            }
                        }
                    }
                }
            }
        }
        Log.i(TAG, "Consultation SDP: remote RTP = $consultRemoteRtpHost:$consultRemoteRtpPort codec=$consultNegotiatedCodec")
    }

    private fun startConsultRtp() {
        if (consultRemoteRtpHost.isBlank() || consultRemoteRtpPort == 0) {
            Log.e(TAG, "Cannot start consultation RTP: no remote endpoint")
            return
        }
        consultRtpSession?.stop()
        val sock = consultRtpSocket
        consultRtpSocket = null
        consultRtpSession = RtpSession(consultRtpPort, consultRemoteRtpHost, consultRemoteRtpPort, sock, consultNegotiatedCodec)
        consultRtpSession?.start()
        Log.i(TAG, "Consultation RTP started: local=$consultRtpPort remote=$consultRemoteRtpHost:$consultRemoteRtpPort")
    }

    // ==================== REQUEST HANDLING ====================

    private fun handleRequest(message: String) {
        val method = message.split(" ").firstOrNull() ?: return
        val msgCallId = extractHeader(message, "Call-ID") ?: ""

        when (method) {
            "INVITE" -> {
                if (msgCallId == currentCallId && _callState.value in listOf(CallState.CONFIRMED, CallState.HOLD)) {
                    handleReInvite(message)
                } else if (msgCallId == currentCallId && _callState.value == CallState.INCOMING) {
                    // INVITE retransmission while ringing — re-send 180 Ringing
                    Log.i(TAG, "INVITE retransmission (same Call-ID) — re-sending 180 Ringing")
                    val response = buildMirroredResponse(180, "Ringing", message, currentLocalTag)
                    sendSip(response)
                } else if (msgCallId == consultCallId && consultCallId.isNotEmpty()) {
                    // Re-INVITE on consultation call - respond with 200 OK
                    handleConsultReInvite(message)
                } else {
                    handleIncomingInvite(message)
                }
            }
            "ACK" -> { /* ACK received, nothing to do */ }
            "BYE" -> {
                // Always respond 200 OK to BYE (even if call already ended)
                val byeResponse = buildMirroredResponse(200, "OK", message)
                sendSip(byeResponse)
                if (msgCallId == consultCallId && consultCallId.isNotEmpty()) {
                    // BYE for consultation call (e.g. after attended transfer completes)
                    Log.i(TAG, "Consultation call BYE received")
                    cleanupConsultation()
                } else if (msgCallId == currentCallId && _callState.value != CallState.IDLE) {
                    handleIncomingBye(message)
                }
            }
            "CANCEL" -> {
                if (msgCallId == currentCallId) {
                    handleIncomingCancel(message)
                }
            }
            "OPTIONS" -> {
                val response = buildMirroredResponse(200, "OK", message)
                sendSip(response)
            }
            "NOTIFY" -> {
                handleNotify(message)
            }
            "MESSAGE" -> {
                handleIncomingMessage(message)
            }
        }
    }

    private fun handleIncomingMessage(message: String) {
        // Send 200 OK immediately
        val response = buildMirroredResponse(200, "OK", message)
        sendSip(response)

        // Extract sender from From header: "Name" <sip:user@domain>;tag=xxx
        val fromHeader = extractHeader(message, "From") ?: ""
        val fromUser = Regex("sip:([^@]+)@").find(fromHeader)?.groupValues?.get(1) ?: "unknown"

        // Extract message body
        val body = extractSipBody(message)
        if (body.isBlank()) {
            Log.d(TAG, "Received empty MESSAGE from $fromUser — ignoring")
            return
        }

        Log.i(TAG, "Received MESSAGE from $fromUser: ${body.take(100)}")
        onMessageReceived?.invoke(fromUser, body)
    }

    private fun handleIncomingInvite(message: String) {
        if (_callState.value != CallState.IDLE) {
            val response = buildMirroredResponse(486, "Busy Here", message, generateTag())
            sendSip(response)
            return
        }

        incomingInviteMsg = message
        currentCallId = extractHeader(message, "Call-ID") ?: ""
        currentLocalTag = generateTag()
        currentRemoteTag = extractTagFromHeader(message, "From") ?: ""
        currentCallDirection = "inbound"
        currentCSeq = 1

        val from = extractHeader(message, "From") ?: ""
        _remoteNumber.value = extractNumberFromUri(from)
        _remoteName.value = extractNameFromHeader(from)

        // Set remote URI from Contact header (for BYE Request-URI) or construct from From
        val contactHeader = extractHeader(message, "Contact") ?: ""
        val contactUri = Regex("<(sip:[^>]+)>").find(contactHeader)?.groupValues?.get(1)
        currentRemoteUri = contactUri ?: "sip:${_remoteNumber.value}@${config.domain}"

        // Parse SDP for remote RTP address
        parseSdpFromMessage(message)

        // Allocate local RTP port
        localRtpPort = allocateRtpPort()

        _callState.value = CallState.INCOMING
        onCallStateChanged?.invoke(CallState.INCOMING, _remoteNumber.value, _remoteName.value)

        // Send 180 Ringing
        val response = buildMirroredResponse(180, "Ringing", message, currentLocalTag)
        sendSip(response)
    }

    private fun handleReInvite(message: String) {
        // Update remote RTP if new SDP provided
        parseSdpFromMessage(message)

        // Respond with 200 OK and our SDP
        val sdp = if (_callState.value == CallState.HOLD)
            buildSdp(localRtpPort, holdMode = true)
        else
            buildSdp(localRtpPort)

        val viaHeader = extractHeader(message, "Via") ?: ""
        val fromHeader = extractHeader(message, "From") ?: ""
        val toHeader = extractHeader(message, "To") ?: ""
        val cseqHeader = extractHeader(message, "CSeq") ?: ""

        val toWithTag = if (!toHeader.contains("tag=") && currentLocalTag.isNotBlank())
            "$toHeader;tag=$currentLocalTag" else toHeader

        val response = "SIP/2.0 200 OK\r\n" +
            "Via: $viaHeader\r\n" +
            "From: $fromHeader\r\n" +
            "To: $toWithTag\r\n" +
            "Call-ID: $currentCallId\r\n" +
            "CSeq: $cseqHeader\r\n" +
            "Contact: <sip:${config.username}@${contactAddress()}>\r\n" +
            "Content-Type: application/sdp\r\n" +
            "User-Agent: MyLineTelecom-Android/1.0\r\n" +
            "Content-Length: ${sdp.toByteArray().size}\r\n\r\n" +
            sdp

        sendSip(response)
        Log.i(TAG, "Re-INVITE 200 OK sent")
    }

    private fun handleConsultReInvite(message: String) {
        parseConsultSdpFromMessage(message)

        val sdp = buildSdp(consultRtpPort)
        val viaHeader = extractHeader(message, "Via") ?: ""
        val fromHeader = extractHeader(message, "From") ?: ""
        val toHeader = extractHeader(message, "To") ?: ""
        val cseqHeader = extractHeader(message, "CSeq") ?: ""

        val toWithTag = if (!toHeader.contains("tag=") && consultLocalTag.isNotBlank())
            "$toHeader;tag=$consultLocalTag" else toHeader

        val response = "SIP/2.0 200 OK\r\n" +
            "Via: $viaHeader\r\n" +
            "From: $fromHeader\r\n" +
            "To: $toWithTag\r\n" +
            "Call-ID: $consultCallId\r\n" +
            "CSeq: $cseqHeader\r\n" +
            "Contact: <sip:${config.username}@${contactAddress()}>\r\n" +
            "Content-Type: application/sdp\r\n" +
            "User-Agent: MyLineTelecom-Android/1.0\r\n" +
            "Content-Length: ${sdp.toByteArray().size}\r\n\r\n" +
            sdp

        sendSip(response)
        Log.i(TAG, "Consultation Re-INVITE 200 OK sent")
    }

    private fun handleIncomingBye(message: String) {
        // 200 OK already sent by caller
        rtpSession?.stop()
        rtpSession = null
        _callState.value = CallState.DISCONNECTED
        onCallStateChanged?.invoke(CallState.DISCONNECTED, _remoteNumber.value, _remoteName.value)
        scope.launch { delay(1000); resetCallState() }
    }

    private fun handleIncomingCancel(message: String) {
        // 200 OK to CANCEL
        val cancelResponse = buildMirroredResponse(200, "OK", message)
        sendSip(cancelResponse)

        // 487 to original INVITE
        val invite = incomingInviteMsg
        if (invite != null) {
            val inviteResponse = buildMirroredResponse(487, "Request Terminated", invite, currentLocalTag)
            sendSip(inviteResponse)
        }

        _callState.value = CallState.DISCONNECTED
        onCallStateChanged?.invoke(CallState.DISCONNECTED, _remoteNumber.value, _remoteName.value)
        scope.launch { delay(1000); resetCallState() }
    }

    private fun handleNotify(message: String) {
        val event = extractHeader(message, "Event") ?: ""
        val notifyCallId = extractHeader(message, "Call-ID") ?: ""

        // Handle REFER progress (sipfrag) — always accept
        if (event.contains("refer")) {
            val response = buildMirroredResponse(200, "OK", message)
            sendSip(response)
            val body = extractSipBody(message)
            Log.i(TAG, "Transfer NOTIFY: $body")
            // Check if transfer completed (200 OK in sipfrag body)
            if (body.contains("SIP/2.0 200")) {
                Log.i(TAG, "Transfer completed successfully, ending our leg")
                // Transfer succeeded - hang up our side
                scope.launch(Dispatchers.IO) {
                    delay(500)
                    hangup()
                }
            }
            return
        }

        // For dialog-event (BLF) NOTIFYs, check if Call-ID matches a known subscription.
        // After network changes, the server may keep sending NOTIFYs for old (stale)
        // subscription Call-IDs. Respond 481 to tell the server to stop.
        if (event.contains("dialog") && !blfSubscriptions.containsKey(notifyCallId)) {
            val response = buildMirroredResponse(481, "Subscription does not exist", message)
            sendSip(response)
            if (rejectedBlfCallIds.add(notifyCallId)) {
                Log.d(TAG, "NOTIFY for unknown BLF Call-ID $notifyCallId — sending 481")
            }
            // Silently reject repeats (no log spam)
            return
        }

        // Known subscription or non-dialog event — accept
        val response = buildMirroredResponse(200, "OK", message)
        sendSip(response)

        // Parse BLF state
        if (!event.contains("dialog")) return

        val body = extractSipBody(message)
        if (body.isBlank()) return

        Log.d(TAG, "BLF NOTIFY body: $body")

        try {
            val entityMatch = Regex("entity=\"sip:([^@]+)@").find(body)
            val extension = entityMatch?.groupValues?.get(1) ?: return

            // Parse each <dialog> element and find the most "active" state
            // Priority: confirmed/trying > early > terminated/no dialog (idle)
            val dialogPattern = Regex("<dialog[\\s][^>]*>(.*?)</dialog>", RegexOption.DOT_MATCHES_ALL)
            val dialogs = dialogPattern.findAll(body).toList()

            val state = if (dialogs.isEmpty()) {
                // No dialog elements = extension is idle
                BlfState.IDLE
            } else {
                // Check each dialog for its state - use the most "active" one
                var bestState = BlfState.IDLE
                for (dialog in dialogs) {
                    val dialogContent = dialog.value
                    // Check <state> child element
                    val stateMatch = Regex("<state>([^<]+)</state>").find(dialogContent)
                    val dialogState = stateMatch?.groupValues?.get(1)?.trim()?.lowercase()

                    // Also check state attribute on <dialog> tag itself (not <dialog-info>)
                    val attrMatch = Regex("<dialog[\\s][^>]+state=\"([^\"]+)\"").find(dialogContent)
                    val attrState = attrMatch?.groupValues?.get(1)?.trim()?.lowercase()

                    Log.d(TAG, "BLF dialog for $extension: state=$dialogState, attr=$attrState")

                    // Skip terminated dialogs
                    if (attrState == "terminated" || dialogState == "terminated") continue

                    val thisState = when (dialogState) {
                        "confirmed" -> BlfState.BUSY
                        "trying" -> BlfState.BUSY
                        "early" -> BlfState.RINGING
                        "proceeding" -> BlfState.RINGING
                        else -> when (attrState) {
                            "confirmed" -> BlfState.BUSY
                            "trying" -> BlfState.BUSY
                            "early" -> BlfState.RINGING
                            else -> BlfState.IDLE
                        }
                    }

                    // Keep the most active state
                    if (thisState == BlfState.BUSY || (thisState == BlfState.RINGING && bestState != BlfState.BUSY)) {
                        bestState = thisState
                    }
                }
                bestState
            }

            Log.i(TAG, "BLF state for $extension: $state")
            _blfStates.value = _blfStates.value.toMutableMap().apply { put(extension, state) }
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing BLF notify", e)
        }
    }

    // ==================== SIP MESSAGE BUILDING ====================

    private fun buildRequest(
        method: String,
        requestUri: String,
        toUri: String,
        fromUri: String,
        callId: String,
        cseq: Int,
        fromTag: String,
        toTag: String = "",
        extraHeaders: Map<String, String> = emptyMap(),
        body: String = "",
        viaBranch: String = ""
    ): String {
        val branch = if (viaBranch.isNotEmpty()) viaBranch else "z9hG4bK${Random.nextInt(100000, 999999)}"
        val via = buildString {
            append("SIP/2.0/${config.transport} $localIp:${config.localPort};branch=$branch")
            if (config.rport) append(";rport")
        }

        val displayName = if (config.displayName.isNotBlank()) "\"${config.displayName}\" " else ""
        val toTagStr = if (toTag.isNotEmpty()) ";tag=$toTag" else ""

        val sb = StringBuilder()
        sb.append("$method $requestUri SIP/2.0\r\n")
        sb.append("Via: $via\r\n")
        sb.append("Max-Forwards: 70\r\n")
        sb.append("From: $displayName<$fromUri>;tag=$fromTag\r\n")
        sb.append("To: <$toUri>$toTagStr\r\n")
        sb.append("Call-ID: $callId\r\n")
        sb.append("CSeq: $cseq $method\r\n")
        sb.append("User-Agent: MyLineTelecom-Android/1.0\r\n")

        for ((key, value) in extraHeaders) {
            sb.append("$key: $value\r\n")
        }

        sb.append("Content-Length: ${body.toByteArray().size}\r\n\r\n")
        if (body.isNotEmpty()) sb.append(body)

        return sb.toString()
    }

    /**
     * Build a response that mirrors the Via/From/To/CSeq from an incoming request.
     * This is critical for proper SIP dialog matching.
     */
    private fun buildMirroredResponse(
        statusCode: Int,
        statusText: String,
        request: String,
        toTag: String = ""
    ): String {
        val via = extractHeader(request, "Via") ?: ""
        val from = extractHeader(request, "From") ?: ""
        val to = extractHeader(request, "To") ?: ""
        val callId = extractHeader(request, "Call-ID") ?: ""
        val cseq = extractHeader(request, "CSeq") ?: ""

        val toWithTag = if (toTag.isNotEmpty() && !to.contains("tag="))
            "$to;tag=$toTag" else to

        return "SIP/2.0 $statusCode $statusText\r\n" +
            "Via: $via\r\n" +
            "From: $from\r\n" +
            "To: $toWithTag\r\n" +
            "Call-ID: $callId\r\n" +
            "CSeq: $cseq\r\n" +
            "User-Agent: MyLineTelecom-Android/1.0\r\n" +
            "Content-Length: 0\r\n\r\n"
    }

    private fun sendAck(responseMessage: String, isNon2xx: Boolean = false) {
        val toHeader = extractHeader(responseMessage, "To") ?: ""
        val remoteTag = extractTagFromHeaderValue(toHeader) ?: currentRemoteTag
        val requestUri = currentRemoteUri.ifEmpty { "sip:${config.domain}" }

        // RFC 3261: ACK for non-2xx MUST use the same Via branch as the original INVITE
        // ACK for 2xx uses a new branch (it's a new transaction)
        val branch = if (isNon2xx) extractViaBranch(responseMessage) else ""

        val request = buildRequest(
            method = "ACK",
            requestUri = requestUri,
            toUri = currentRemoteUri.ifEmpty { "sip:${config.domain}" },
            fromUri = "sip:${config.username}@${config.domain}",
            callId = currentCallId,
            cseq = currentCSeq,
            fromTag = currentLocalTag,
            toTag = remoteTag,
            viaBranch = branch
        )
        sendSip(request)
    }

    private fun extractViaBranch(message: String): String {
        val viaHeader = extractHeader(message, "Via") ?: return ""
        val branchMatch = Regex("branch=([^;,\\s]+)").find(viaHeader)
        return branchMatch?.groupValues?.get(1) ?: ""
    }

    private fun resendInviteWithAuth(isProxy: Boolean) {
        val contactAddr = contactAddress()
        val authHeader = buildAuthHeader("INVITE", currentRemoteUri)
        val headerName = if (isProxy) "Proxy-Authorization" else "Authorization"
        // Preserve hold mode when resending re-INVITE with auth
        val sdp = buildSdp(localRtpPort, holdMode = pendingHoldMode)

        // Generate new branch for the auth-retried INVITE (new transaction)
        currentInviteBranch = "z9hG4bK${Random.nextInt(100000, 999999)}"

        val request = buildRequest(
            method = "INVITE",
            requestUri = currentRemoteUri,
            toUri = currentRemoteUri,
            fromUri = "sip:${config.username}@${config.domain}",
            callId = currentCallId,
            cseq = currentCSeq,
            fromTag = currentLocalTag,
            toTag = currentRemoteTag,
            extraHeaders = mapOf(
                "Contact" to "<sip:${config.username}@$contactAddr>",
                "Content-Type" to "application/sdp",
                "Allow" to "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER",
                headerName to authHeader
            ),
            body = sdp,
            viaBranch = currentInviteBranch
        )
        sendSip(request)
    }

    // ==================== SDP ====================

    private fun buildSdp(rtpPort: Int, holdMode: Boolean = false): String {
        val ip = if (publicIp.isNotEmpty()) publicIp else localIp
        val sessionId = System.currentTimeMillis() / 1000
        val mode = if (holdMode) "a=sendonly" else "a=sendrecv"

        // Offer codecs: PCMU, G.729, GSM, and DTMF
        val g729Available = G729Codec.isAvailable()
        val codecList = if (g729Available) "0 18 3 101" else "0 3 101"
        val g729Line = if (g729Available) "a=rtpmap:18 G729/8000\r\na=fmtp:18 annexb=no\r\n" else ""

        return "v=0\r\n" +
            "o=${config.username} $sessionId $sessionId IN IP4 $ip\r\n" +
            "s=MyLineTelecom\r\n" +
            "c=IN IP4 $ip\r\n" +
            "t=0 0\r\n" +
            "m=audio $rtpPort RTP/AVP $codecList\r\n" +
            "a=rtpmap:0 PCMU/8000\r\n" +
            g729Line +
            "a=rtpmap:3 GSM/8000\r\n" +
            "a=rtpmap:101 telephone-event/8000\r\n" +
            "a=fmtp:101 0-16\r\n" +
            "a=ptime:20\r\n" +
            "$mode\r\n"
    }

    private fun extractSipBody(message: String): String {
        // Handle both \r\n\r\n and \n\n header/body separators
        val body = message.substringAfter("\r\n\r\n", "").ifEmpty {
            message.substringAfter("\n\n", "")
        }
        return body
    }

    private fun parseSdpFromMessage(message: String) {
        val body = extractSipBody(message)
        if (body.isBlank()) return

        for (line in body.lines()) {
            when {
                line.startsWith("c=IN IP4 ") -> {
                    remoteRtpHost = line.substringAfter("c=IN IP4 ").trim()
                }
                line.startsWith("m=audio ") -> {
                    // m=audio PORT RTP/AVP codec1 codec2 ...
                    val parts = line.split(" ")
                    if (parts.size >= 2) {
                        remoteRtpPort = parts[1].toIntOrNull() ?: 0
                    }
                    // Pick first audio codec (skip DTMF payload types like 101)
                    if (parts.size >= 4) {
                        for (i in 3 until parts.size) {
                            val pt = parts[i].trim().toIntOrNull() ?: continue
                            if (pt in listOf(0, 3, 8, 18)) { // PCMU=0, GSM=3, PCMA=8, G729=18
                                negotiatedCodec = pt
                                break
                            }
                        }
                    }
                }
            }
        }
        val codecName = when (negotiatedCodec) { 0 -> "PCMU"; 3 -> "GSM"; 8 -> "PCMA"; 18 -> "G729"; else -> "PT$negotiatedCodec" }
        Log.i(TAG, "Parsed SDP: remote RTP = $remoteRtpHost:$remoteRtpPort codec=$codecName")
    }

    // ==================== AUTH ====================

    private fun parseAuthChallenge(message: String) {
        val header = extractHeader(message, "WWW-Authenticate")
            ?: extractHeader(message, "Proxy-Authenticate")
            ?: return

        authRealm = extractQuotedParam(header, "realm")
        authNonce = extractQuotedParam(header, "nonce")
        authOpaque = extractQuotedParam(header, "opaque")
        authAlgorithm = extractUnquotedParam(header, "algorithm").ifEmpty { "MD5" }
        authQop = extractQuotedParam(header, "qop").ifEmpty { extractUnquotedParam(header, "qop") }

        if (authQop.isNotEmpty()) {
            authNonceCount = 1
            authCNonce = Random.nextInt(100000, 999999).toString()
        }
    }

    private fun buildAuthHeader(method: String, uri: String): String {
        val ha1 = md5("${config.username}:$authRealm:${config.password}")
        val ha2 = md5("$method:$uri")

        val response = if (authQop.isNotEmpty()) {
            val nc = String.format("%08x", authNonceCount)
            authNonceCount++
            md5("$ha1:$authNonce:$nc:$authCNonce:auth:$ha2")
        } else {
            md5("$ha1:$authNonce:$ha2")
        }

        val sb = StringBuilder()
        sb.append("Digest username=\"${config.username}\", realm=\"$authRealm\", nonce=\"$authNonce\", uri=\"$uri\", response=\"$response\"")
        if (authOpaque.isNotEmpty()) sb.append(", opaque=\"$authOpaque\"")
        sb.append(", algorithm=$authAlgorithm")
        if (authQop.isNotEmpty()) {
            sb.append(", qop=auth, nc=${String.format("%08x", authNonceCount - 1)}, cnonce=\"$authCNonce\"")
        }
        return sb.toString()
    }

    // ==================== NETWORKING ====================

    /**
     * Send SIP message synchronously. Must be called from IO dispatcher.
     */
    private fun sendSip(message: String) {
        try {
            val bytes = message.toByteArray()
            val address = InetAddress.getByName(config.domain)
            val packet = DatagramPacket(bytes, bytes.size, address, config.port)
            socket?.send(packet)
            Log.d(TAG, ">>> Sent to ${config.domain}:${config.port}:\n${message.take(400)}")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send SIP message to ${config.domain}:${config.port} socket=${socket != null} closed=${socket?.isClosed}: ${e.message}", e)
        }
    }

    private fun startRtp() {
        if (remoteRtpHost.isBlank() || remoteRtpPort == 0) {
            Log.e(TAG, "Cannot start RTP: no remote endpoint ($remoteRtpHost:$remoteRtpPort)")
            return
        }
        rtpSession?.stop()
        // Pass the pre-opened socket so RTP doesn't need to re-bind the port
        val sock = rtpSocket
        rtpSocket = null // Transfer ownership to RtpSession
        rtpSession = RtpSession(localRtpPort, remoteRtpHost, remoteRtpPort, sock, negotiatedCodec)
        rtpSession?.start()
        val codecName = when (negotiatedCodec) { 0 -> "PCMU"; 3 -> "GSM"; 8 -> "PCMA"; 18 -> "G729"; else -> "PT$negotiatedCodec" }
        Log.i(TAG, "RTP started: local=$localRtpPort remote=$remoteRtpHost:$remoteRtpPort codec=$codecName")
    }

    // ==================== STUN / NAT ====================

    private fun discoverPublicAddress() {
        try {
            val result = StunClient.discover(socket!!)
            if (result != null) {
                publicIp = result.first
                publicPort = result.second
                Log.i(TAG, "STUN discovered: $publicIp:$publicPort")
            } else {
                Log.w(TAG, "STUN discovery returned null — will learn public address from server response")
            }
        } catch (e: Exception) {
            Log.w(TAG, "STUN discovery failed: ${e.message} — will learn public address from server response")
        }
    }

    /**
     * Learn our public IP/port from the Via header of a server response.
     * The server adds received= and rport= parameters showing where the packet
     * actually came from. This is a reliable fallback when STUN fails.
     */
    private fun learnPublicAddressFromResponse(message: String) {
        val via = extractHeader(message, "Via") ?: return
        val receivedIp = Regex("received=([^;\\s]+)").find(via)?.groupValues?.get(1) ?: return
        val receivedPort = Regex("rport=([0-9]+)").find(via)?.groupValues?.get(1)?.toIntOrNull() ?: return

        if (publicIp.isEmpty() || publicPort == 0) {
            publicIp = receivedIp
            publicPort = receivedPort
            Log.i(TAG, "Learned public address from server: $publicIp:$publicPort")
        }
    }

    // ==================== KEEPALIVE / RE-REGISTER ====================

    private fun checkNatChanged(message: String) {
        val via = extractHeader(message, "Via") ?: return
        val receivedMatch = Regex("received=([^;\\s]+)").find(via)
        val rportMatch = Regex("rport=([0-9]+)").find(via)
        val receivedIp = receivedMatch?.groupValues?.get(1) ?: return
        val receivedPort = rportMatch?.groupValues?.get(1)?.toIntOrNull() ?: return

        if (publicIp.isNotEmpty() && (receivedIp != publicIp || receivedPort != publicPort)) {
            Log.w(TAG, "NAT mapping changed: $publicIp:$publicPort → $receivedIp:$receivedPort — forcing re-registration")
            restartForNetworkChange()
        }
    }

    private fun startKeepalive() {
        keepaliveJob?.cancel()
        pendingKeepalives = 0
        lastKeepaliveCallId = ""
        keepaliveJob = scope.launch(Dispatchers.IO) {
            while (isActive) {
                delay(KEEPALIVE_INTERVAL)
                if (_registrationState.value == RegistrationState.REGISTERED) {
                    // If previous keepalives went unanswered, the server is unreachable —
                    // our registration is stale (likely NAT pinhole closed). Force re-register.
                    if (pendingKeepalives >= 2) {
                        Log.w(TAG, "Keepalive: server hasn't responded to $pendingKeepalives OPTIONS — registration is stale, forcing re-registration")
                        restartForNetworkChange()
                        return@launch
                    }

                    // Send OPTIONS as keepalive
                    val optCallId = generateCallId()
                    lastKeepaliveCallId = optCallId
                    pendingKeepalives++
                    val request = buildRequest(
                        method = "OPTIONS",
                        requestUri = "sip:${config.domain}",
                        toUri = "sip:${config.domain}",
                        fromUri = "sip:${config.username}@${config.domain}",
                        callId = optCallId,
                        cseq = 1,
                        fromTag = generateTag()
                    )
                    sendSip(request)
                }
            }
        }
    }

    private fun scheduleReRegister() {
        reRegisterJob?.cancel()
        reRegisterJob = scope.launch(Dispatchers.IO) {
            delay(REGISTER_EXPIRES * 1000L - RE_REGISTER_BEFORE)
            if (isActive && _registrationState.value == RegistrationState.REGISTERED) {
                Log.i(TAG, "Re-registering...")
                registerWithAuth(false)
            }
        }
    }

    // ==================== HELPERS ====================

    private fun resetCallState() {
        currentCallId = ""
        currentLocalTag = ""
        currentRemoteTag = ""
        currentRemoteUri = ""
        currentCallDirection = ""
        currentCSeq = 1
        currentInviteBranch = ""
        incomingInviteMsg = null
        inviteAuthAttempted = false
        pendingHoldMode = false
        pendingReferTarget = ""
        referAuthAttempted = false
        localRtpPort = 0
        remoteRtpHost = ""
        remoteRtpPort = 0
        negotiatedCodec = 0
        rtpSession?.stop()
        rtpSession = null
        rtpSocket?.close()
        rtpSocket = null

        // Clean up any active consultation call
        cleanupConsultation()

        _remoteNumber.value = ""
        _remoteName.value = ""
        _callState.value = CallState.IDLE
    }

    private fun contactAddress(): String {
        val ip = if (publicIp.isNotEmpty()) publicIp else localIp
        val port = if (publicPort > 0) publicPort else config.localPort
        return "$ip:$port"
    }

    fun getCurrentLocalIp(): String = getLocalIpAddress()

    private fun getLocalIpAddress(): String {
        try {
            var wifiIp: String? = null
            var cellularIp: String? = null
            var fallbackIp: String? = null

            val interfaces = java.net.NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val name = iface.name.lowercase()
                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (!addr.isLoopbackAddress && addr is java.net.Inet4Address) {
                        val ip = addr.hostAddress ?: continue
                        when {
                            // Wi-Fi interfaces: wlan0, wlan1, etc.
                            name.startsWith("wlan") -> wifiIp = ip
                            // Cellular interfaces: rmnet, ccmni, pdp, etc.
                            name.startsWith("rmnet") || name.startsWith("ccmni") || name.startsWith("pdp") -> cellularIp = ip
                            // Any other valid interface
                            else -> if (fallbackIp == null) fallbackIp = ip
                        }
                    }
                }
            }

            // Prefer Wi-Fi, then cellular, then any available
            val selected = wifiIp ?: cellularIp ?: fallbackIp ?: "0.0.0.0"
            Log.i(TAG, "Network: wifi=$wifiIp cellular=$cellularIp fallback=$fallbackIp → using $selected")
            return selected
        } catch (e: Exception) {
            Log.e(TAG, "Failed to get local IP", e)
        }
        return "0.0.0.0"
    }

    private fun allocateRtpPort(): Int {
        // Close any previously allocated RTP socket
        rtpSocket?.close()
        rtpSocket = null

        // Find an available even port and keep the socket open
        for (port in 10000..20000 step 2) {
            try {
                rtpSocket = DatagramSocket(port)
                Log.i(TAG, "RTP port allocated: $port")
                return port
            } catch (_: Exception) { }
        }
        // Fallback - let OS assign
        rtpSocket = DatagramSocket()
        val port = rtpSocket!!.localPort
        Log.w(TAG, "Using OS-assigned RTP port: $port")
        return port
    }

    // ==================== STRING UTILS ====================

    private fun extractHeader(message: String, headerName: String): String? {
        for (line in message.lines()) {
            if (line.startsWith("$headerName:", ignoreCase = true)) {
                return line.substringAfter(":").trim()
            }
        }
        return null
    }

    private fun extractTagFromHeader(message: String, headerName: String): String? {
        val header = extractHeader(message, headerName) ?: return null
        return extractTagFromHeaderValue(header)
    }

    private fun extractTagFromHeaderValue(headerValue: String): String? {
        val match = Regex("tag=([^;>\\s]+)").find(headerValue)
        return match?.groupValues?.get(1)
    }

    private fun extractNumberFromUri(text: String): String {
        return Regex("sip:([^@>]+)@").find(text)?.groupValues?.get(1)
            ?: Regex("sip:([^>]+)").find(text)?.groupValues?.get(1)
            ?: ""
    }

    private fun extractNameFromHeader(header: String): String {
        return Regex("\"([^\"]+)\"").find(header)?.groupValues?.get(1) ?: ""
    }

    private fun extractQuotedParam(header: String, param: String): String {
        val match = Regex("$param=\"([^\"]+)\"", RegexOption.IGNORE_CASE).find(header)
        return match?.groupValues?.get(1) ?: ""
    }

    private fun extractUnquotedParam(header: String, param: String): String {
        val match = Regex("$param=([^,\\s]+)", RegexOption.IGNORE_CASE).find(header)
        return match?.groupValues?.get(1) ?: ""
    }

    private fun generateCallId(): String = "${Random.nextInt(100000, 999999)}@$localIp"
    private fun generateTag(): String = Random.nextInt(100000, 999999).toString()

    private fun md5(input: String): String {
        val md = MessageDigest.getInstance("MD5")
        return md.digest(input.toByteArray()).joinToString("") { "%02x".format(it) }
    }
}
