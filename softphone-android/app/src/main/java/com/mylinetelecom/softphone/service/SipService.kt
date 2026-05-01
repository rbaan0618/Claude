package com.mylinetelecom.softphone.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.mylinetelecom.softphone.MainActivity
import com.mylinetelecom.softphone.R
import com.mylinetelecom.softphone.data.AppDatabase
import com.mylinetelecom.softphone.data.SettingsRepository
import com.mylinetelecom.softphone.models.*
import com.mylinetelecom.softphone.sip.SipHandler
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlin.math.sin

class SipService : Service() {
    companion object {
        private const val TAG = "SipService"
        private const val CHANNEL_CALL = "active_call"
        private const val CHANNEL_SERVICE = "sip_service"
        private const val CHANNEL_MESSAGE = "sip_message"
        private const val NOTIFICATION_ID = 1
        private const val NOTIFICATION_INCOMING_ID = 2
        private const val NOTIFICATION_MESSAGE_ID = 3
    }

    inner class LocalBinder : Binder() {
        fun getService(): SipService = this@SipService
    }

    private val binder = LocalBinder()
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val messageMutex = Mutex()  // serialise inbound message processing to prevent dedup race

    lateinit var sipHandler: SipHandler
        private set
    private lateinit var settingsRepo: SettingsRepository
    private lateinit var database: AppDatabase

    private var wakeLock: PowerManager.WakeLock? = null
    private var serviceWakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var ringtoneJob: Job? = null
    private var ringtoneTrack: AudioTrack? = null
    private var ringbackJob: Job? = null
    private var ringbackTrack: AudioTrack? = null

    // Compose-observable state (directly readable by UI without flow collection)
    var composeCallState by mutableStateOf(CallState.IDLE)
        private set
    var composeRegistrationState by mutableStateOf(RegistrationState.UNREGISTERED)
        private set
    var composeRemoteNumber by mutableStateOf("")
        private set
    var composeRemoteName by mutableStateOf("")
        private set
    var composeCallDuration by mutableStateOf(0)
        private set
    var composeBlfStates by mutableStateOf<Map<String, BlfState>>(emptyMap())
        private set
    var composeIsConsulting by mutableStateOf(false)
        private set
    var composeConsultState by mutableStateOf(CallState.IDLE)
        private set
    var composeConsultNumber by mutableStateOf("")
        private set

    // Call timer
    private val _callDuration = MutableStateFlow(0)
    val callDuration: StateFlow<Int> = _callDuration
    private var timerJob: Job? = null
    private var callStartTime: Long = 0

    // Current call record tracking
    private var currentCallRecordId: Long? = null

    // Network change detection
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var currentNetwork: Network? = null // Track actual Network object, not just type
    private var currentNetworkIp: String? = null // Track IP to detect WiFi roaming
    private var networkChangeJob: Job? = null
    private var sipStarted = false // Guard against restart before first registration

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()

        database = AppDatabase.getInstance(this)
        settingsRepo = SettingsRepository(this)
        sipHandler = SipHandler(serviceScope)

        sipHandler.onCallStateChanged = { state, number, name ->
            serviceScope.launch {
                handleCallStateChange(state, number, name)
            }
        }

        sipHandler.onMessageReceived = { from, body ->
            serviceScope.launch {
                messageMutex.withLock {
                // Deduplicate: skip if identical message from same number arrived within 5 seconds
                // Mutex ensures concurrent SIP retransmissions are serialised — second one
                // will see the first already inserted and be dropped.
                val duplicateCount = database.chatMessageDao().countRecentDuplicates(
                    number = from,
                    body = body,
                    since = System.currentTimeMillis() - 5000L
                )
                if (duplicateCount > 0) {
                    Log.w(TAG, "Duplicate inbound message from $from — ignored")
                    return@withLock
                }

                // WhatsApp inbound arrives with '+' prefix (e.g. +13059684280)
                // SMS inbound arrives without '+' (e.g. 13059684280)
                val msgType = if (from.startsWith("+")) "whatsapp" else "sms"
                val msg = ChatMessage(
                    remoteNumber = from,
                    body = body,
                    isOutgoing = false,
                    status = MessageStatus.RECEIVED,
                    messageType = msgType
                )
                database.chatMessageDao().insert(msg)
                Log.i(TAG, "Saved incoming $msgType message from $from")
                showMessageNotification(from, body, msgType)
                } // end messageMutex.withLock
            }
        }

        sipHandler.onRegistrationChanged = { state ->
            when (state) {
                RegistrationState.REGISTERED -> {
                    updateServiceNotification("Registered")
                    // Re-subscribe all saved BLF entries after registration
                    serviceScope.launch {
                        val entries = settingsRepo.blfEntries.first()
                        if (entries.isNotEmpty()) {
                            Log.i(TAG, "Re-subscribing ${entries.size} BLF entries after registration")
                            for (entry in entries) {
                                sipHandler.subscribeBlfExtension(entry.extension)
                                delay(100) // Small delay between subscriptions
                            }
                        }
                    }
                }
                RegistrationState.FAILED -> updateServiceNotification("Registration failed")
                RegistrationState.UNREGISTERED -> updateServiceNotification("Not registered")
                else -> {}
            }
        }

        // Bridge SipHandler StateFlows to Compose-observable state
        serviceScope.launch { sipHandler.callState.collect { composeCallState = it } }
        serviceScope.launch { sipHandler.registrationState.collect { composeRegistrationState = it } }
        serviceScope.launch { sipHandler.remoteNumber.collect { composeRemoteNumber = it } }
        serviceScope.launch { sipHandler.remoteName.collect { composeRemoteName = it } }
        serviceScope.launch { sipHandler.blfStates.collect { composeBlfStates = it } }
        serviceScope.launch { callDuration.collect { composeCallDuration = it } }
        serviceScope.launch { sipHandler.isConsulting.collect { composeIsConsulting = it } }
        serviceScope.launch { sipHandler.consultState.collect { composeConsultState = it } }
        serviceScope.launch { sipHandler.consultNumber.collect { composeConsultNumber = it } }

        // Keep WiFi alive in background
        try {
            val wifiManager = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            wifiLock = wifiManager.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MyLineTelecom::SipWifiLock")
            wifiLock?.acquire()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to acquire WiFi lock", e)
        }

        // Keep CPU alive so we can receive SIP messages in background
        try {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            serviceWakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MyLineTelecom::SipServiceLock")
            serviceWakeLock?.acquire()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to acquire service wake lock", e)
        }

        // Monitor network changes — re-register when switching WiFi/cellular
        registerNetworkCallback()

        // Load config and start
        serviceScope.launch {
            settingsRepo.sipConfig.first().let { config ->
                if (config.isValid && config.enabled) {
                    sipHandler.configure(config)
                    sipHandler.start()
                    sipStarted = true
                }
            }
        }
    }

    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager

        // Set initial network BEFORE registering callback to avoid race
        currentNetwork = cm.activeNetwork
        currentNetworkIp = sipHandler.getCurrentLocalIp()
        Log.i(TAG, "Initial network: $currentNetwork (ip=$currentNetworkIp)")

        networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                val netCaps = cm.getNetworkCapabilities(network)
                val newType = when {
                    netCaps == null -> "unknown"
                    netCaps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                    netCaps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                    else -> "other"
                }
                Log.i(TAG, "Default network available: $newType (network=$network, was=$currentNetwork)")

                if (!sipStarted) {
                    Log.i(TAG, "Ignoring network callback — SIP not started yet")
                    currentNetwork = network
                    return
                }

                // Restart when: network object changed, OR recovering from lost network
                if (currentNetwork == null || currentNetwork != network) {
                    Log.i(TAG, "Network changed: $currentNetwork → $network ($newType) — scheduling restart")
                    currentNetwork = network
                    scheduleNetworkRestart()
                } else {
                    currentNetwork = network
                }
            }

            override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
                if (!sipStarted) return

                // Detect IP change on same network (WiFi roaming between APs)
                val newIp = sipHandler.getCurrentLocalIp()
                if (currentNetworkIp != null && newIp != currentNetworkIp && newIp != "0.0.0.0") {
                    Log.i(TAG, "IP changed on same network: $currentNetworkIp → $newIp — scheduling restart")
                    scheduleNetworkRestart()
                }
                currentNetworkIp = newIp
            }

            override fun onLost(network: Network) {
                Log.w(TAG, "Default network lost — waiting for new connection")
                currentNetwork = null
                currentNetworkIp = null
                updateServiceNotification("No network")
            }
        }

        cm.registerDefaultNetworkCallback(networkCallback!!)
    }

    private fun scheduleNetworkRestart() {
        updateServiceNotification("Switching network...")
        networkChangeJob?.cancel()
        networkChangeJob = serviceScope.launch {
            delay(2000)
            Log.i(TAG, "Executing debounced network restart")
            currentNetworkIp = sipHandler.getCurrentLocalIp()
            sipHandler.restartForNetworkChange()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val notification = buildServiceNotification("Starting...")
        startForeground(NOTIFICATION_ID, notification)

        when (intent?.action) {
            "ACTION_ANSWER" -> sipHandler.answerCall()
            "ACTION_DECLINE" -> sipHandler.hangup()
            "ACTION_HANGUP" -> sipHandler.hangup()
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder = binder

    private var shutdownDone = false

    override fun onTaskRemoved(rootIntent: Intent?) {
        // Service should STAY ALIVE when swiped from recents — we are a foreground service
        // Only unregister on explicit exit via the exit button
        Log.i(TAG, "App swiped from recents — service stays alive for incoming calls")
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        Log.i(TAG, "Service destroyed — unregistering from SIP")
        performCleanShutdown()
        super.onDestroy()
    }

    private fun performCleanShutdown() {
        if (shutdownDone) return  // Prevent double cleanup
        shutdownDone = true
        Log.i(TAG, "performCleanShutdown starting")

        stopRingtone()
        stopRingback()
        releaseWakeLock()
        try {
            networkCallback?.let {
                val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
                cm.unregisterNetworkCallback(it)
            }
        } catch (_: Exception) { }
        networkCallback = null
        try { serviceWakeLock?.let { if (it.isHeld) it.release() } } catch (_: Exception) { }
        serviceWakeLock = null
        try { wifiLock?.let { if (it.isHeld) it.release() } } catch (_: Exception) { }
        wifiLock = null

        // Send unregister on a separate thread and WAIT for it to finish
        val unregisterThread = Thread {
            try {
                Log.i(TAG, "stopBlocking thread started")
                sipHandler.stopBlocking()
                Log.i(TAG, "stopBlocking thread completed")
            } catch (e: Exception) {
                Log.e(TAG, "Error during shutdown unregister", e)
            }
        }
        unregisterThread.start()
        try {
            unregisterThread.join(5000) // Wait up to 5 seconds for unregister + auth challenge
        } catch (_: Exception) { }
        Log.i(TAG, "performCleanShutdown done")

        serviceScope.cancel()
        stopSelf()
    }

    fun reconfigure(config: SipConfig) {
        serviceScope.launch {
            sipHandler.stop()
            delay(500)
            if (config.isValid && config.enabled) {
                sipHandler.configure(config)
                sipHandler.start()
            }
        }
    }

    private suspend fun handleCallStateChange(state: CallState, number: String, name: String) {
        when (state) {
            CallState.INCOMING -> {
                acquireWakeLock()
                startRingtone()
                showIncomingCallNotification(number, name)
                // Create call record
                val record = CallRecord(
                    direction = CallDirection.INBOUND,
                    remoteNumber = number,
                    remoteName = name,
                    status = "ringing"
                )
                currentCallRecordId = database.callHistoryDao().insert(record)
            }
            CallState.CALLING -> {
                acquireWakeLock()
                startRingback()
                updateCallNotification("Calling $number...")
                val record = CallRecord(
                    direction = CallDirection.OUTBOUND,
                    remoteNumber = number,
                    remoteName = name,
                    status = "calling"
                )
                currentCallRecordId = database.callHistoryDao().insert(record)
            }
            CallState.RINGING -> {
                updateCallNotification("Ringing $number...")
            }
            CallState.CONFIRMED -> {
                stopRingtone()
                stopRingback()
                cancelIncomingNotification()
                callStartTime = System.currentTimeMillis()
                startCallTimer()
                updateCallNotification("In call with $number")
                updateCallRecord("answered", answeredAt = System.currentTimeMillis())
            }
            CallState.HOLD -> {
                updateCallNotification("On hold - $number")
            }
            CallState.DISCONNECTED, CallState.REJECTED, CallState.BUSY -> {
                stopRingtone()
                stopRingback()
                stopCallTimer()
                releaseWakeLock()
                cancelIncomingNotification()
                updateServiceNotification("Ready")
                val statusText = when (state) {
                    CallState.REJECTED -> "rejected"
                    CallState.BUSY -> "busy"
                    else -> "ended"
                }
                updateCallRecord(
                    statusText,
                    endedAt = System.currentTimeMillis(),
                    duration = if (callStartTime > 0) ((System.currentTimeMillis() - callStartTime) / 1000).toInt() else 0
                )
                currentCallRecordId = null
                callStartTime = 0
                _callDuration.value = 0
            }
            else -> {}
        }
    }

    private suspend fun updateCallRecord(
        status: String,
        answeredAt: Long? = null,
        endedAt: Long? = null,
        duration: Int? = null
    ) {
        val recordId = currentCallRecordId ?: return
        // We need to fetch and update - simplified approach
        withContext(Dispatchers.IO) {
            try {
                val dao = database.callHistoryDao()
                // For simplicity, we store the record at creation and update isn't easily done
                // without a getById query. In production, add @Query("SELECT * FROM call_history WHERE id = :id")
            } catch (e: Exception) {
                Log.e(TAG, "Error updating call record", e)
            }
        }
    }

    // ==================== CALL TIMER ====================

    private fun startCallTimer() {
        timerJob?.cancel()
        timerJob = serviceScope.launch {
            while (isActive) {
                delay(1000)
                _callDuration.value = ((System.currentTimeMillis() - callStartTime) / 1000).toInt()
            }
        }
    }

    private fun stopCallTimer() {
        timerJob?.cancel()
    }

    // ==================== RINGTONE ====================

    private fun startRingtone() {
        stopRingtone()
        ringtoneJob = serviceScope.launch(Dispatchers.IO) {
            try {
                val sampleRate = 8000
                val durationMs = 2000 // 2 second ring, 4 second cycle
                val samples = sampleRate * durationMs / 1000
                val buffer = ShortArray(samples)

                // Generate 1200Hz ringtone (matching Python version)
                for (i in 0 until samples) {
                    val t = i.toFloat() / sampleRate
                    // Ring for 0.5s, silence for 0.5s, repeat
                    val phase = (t * 2) % 1.0
                    val amplitude = if (phase < 0.5) 8000 else 0
                    buffer[i] = (amplitude * sin(2.0 * Math.PI * 1200.0 * t)).toInt().toShort()
                }

                val track = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build()
                    )
                    .setBufferSizeInBytes(buffer.size * 2)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build()

                track.write(buffer, 0, buffer.size)
                track.setLoopPoints(0, buffer.size, -1) // Loop indefinitely
                track.play()
                ringtoneTrack = track

            } catch (e: Exception) {
                Log.e(TAG, "Error starting ringtone", e)
            }
        }
    }

    private fun stopRingtone() {
        ringtoneJob?.cancel()
        try {
            ringtoneTrack?.stop()
            ringtoneTrack?.release()
        } catch (_: Exception) { }
        ringtoneTrack = null
    }

    // ==================== RINGBACK TONE ====================

    private fun startRingback() {
        stopRingback()
        ringbackJob = serviceScope.launch(Dispatchers.IO) {
            try {
                val sampleRate = 8000
                // US ringback: 440Hz + 480Hz, 2s on, 4s off = 6s cycle
                val cycleDurationMs = 6000
                val samples = sampleRate * cycleDurationMs / 1000
                val buffer = ShortArray(samples)

                for (i in 0 until samples) {
                    val t = i.toFloat() / sampleRate
                    if (t < 2.0f) {
                        // 2 seconds of tone (440 + 480 Hz mixed)
                        val tone = sin(2.0 * Math.PI * 440.0 * t) + sin(2.0 * Math.PI * 480.0 * t)
                        buffer[i] = (tone * 4000).toInt().coerceIn(-32768, 32767).toShort()
                    } else {
                        // 4 seconds of silence
                        buffer[i] = 0
                    }
                }

                val track = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build()
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build()
                    )
                    .setBufferSizeInBytes(buffer.size * 2)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build()

                track.write(buffer, 0, buffer.size)
                track.setLoopPoints(0, buffer.size, -1) // Loop indefinitely
                track.play()
                ringbackTrack = track

            } catch (e: Exception) {
                Log.e(TAG, "Error starting ringback", e)
            }
        }
    }

    private fun stopRingback() {
        ringbackJob?.cancel()
        try {
            ringbackTrack?.stop()
            ringbackTrack?.release()
        } catch (_: Exception) { }
        ringbackTrack = null
    }

    // ==================== WAKE LOCK ====================

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "MyLineTelecom::CallWakeLock"
            )
            wakeLock?.acquire(30 * 60 * 1000L) // 30 min max
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
    }

    // ==================== NOTIFICATIONS ====================

    private fun createNotificationChannels() {
        val callChannel = NotificationChannel(
            CHANNEL_CALL,
            getString(R.string.notification_channel_call),
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Notifications for active and incoming calls"
            setSound(null, null)
        }

        val serviceChannel = NotificationChannel(
            CHANNEL_SERVICE,
            getString(R.string.notification_channel_service),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "SIP registration service"
            setShowBadge(false)
        }

        val messageChannel = NotificationChannel(
            CHANNEL_MESSAGE,
            getString(R.string.notification_channel_message),
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Incoming message notifications"
        }

        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(callChannel)
        nm.createNotificationChannel(serviceChannel)
        nm.createNotificationChannel(messageChannel)
    }

    private fun buildServiceNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_SERVICE)
            .setContentTitle("My Line Telecom")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.sym_call_outgoing)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setNumber(0)
            .setBadgeIconType(NotificationCompat.BADGE_ICON_NONE)
            .build()
    }

    private fun updateServiceNotification(text: String) {
        val notification = buildServiceNotification(text)
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, notification)
    }

    fun clearMessageNotification(from: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.cancel(NOTIFICATION_MESSAGE_ID + from.hashCode())
    }

    fun clearAllMessageNotifications() {
        // Cancel all known message notifications by clearing the message channel
        val nm = getSystemService(NotificationManager::class.java)
        nm.activeNotifications.forEach { sbn ->
            if (sbn.notification.channelId == CHANNEL_MESSAGE) {
                nm.cancel(sbn.id)
            }
        }
    }

    private fun showMessageNotification(from: String, body: String, msgType: String = "sms") {
        val intent = Intent(this, MainActivity::class.java).apply {
            action = "ACTION_OPEN_CHAT"
            putExtra("chat_number", from)
            putExtra("chat_type", msgType)
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, NOTIFICATION_MESSAGE_ID, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_MESSAGE)
            .setContentTitle("Message from $from")
            .setContentText(body)
            .setSmallIcon(android.R.drawable.sym_action_email)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .build()

        val nm = getSystemService(NotificationManager::class.java)
        // Use unique ID per sender so multiple conversations show separate notifications
        nm.notify(NOTIFICATION_MESSAGE_ID + from.hashCode(), notification)
    }

    private fun updateCallNotification(text: String) {
        val hangupIntent = Intent(this, SipService::class.java).apply {
            action = "ACTION_HANGUP"
        }
        val hangupPending = PendingIntent.getService(
            this, 1, hangupIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val openIntent = Intent(this, MainActivity::class.java)
        val openPending = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_CALL)
            .setContentTitle("My Line Telecom")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentIntent(openPending)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, getString(R.string.action_hangup), hangupPending)
            .build()

        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, notification)
    }

    private fun showIncomingCallNotification(number: String, name: String) {
        val displayCaller = name.ifBlank { number }

        val answerIntent = Intent(this, com.mylinetelecom.softphone.MainActivity::class.java).apply {
            action = "ACTION_ANSWER"
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val answerPending = PendingIntent.getActivity(
            this, 2, answerIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val declineIntent = Intent(this, SipService::class.java).apply {
            action = "ACTION_DECLINE"
        }
        val declinePending = PendingIntent.getService(
            this, 3, declineIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val openIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val openPending = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_CALL)
            .setContentTitle("Incoming Call")
            .setContentText(displayCaller)
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentIntent(openPending)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setFullScreenIntent(openPending, true)
            .addAction(android.R.drawable.sym_call_incoming, getString(R.string.action_answer), answerPending)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, getString(R.string.action_decline), declinePending)
            .build()

        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_INCOMING_ID, notification)
    }

    private fun cancelIncomingNotification() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.cancel(NOTIFICATION_INCOMING_ID)
    }
}
