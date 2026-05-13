package com.mylinetelecom.softphone

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.mylinetelecom.softphone.data.AppDatabase
import com.mylinetelecom.softphone.data.SettingsRepository
import com.mylinetelecom.softphone.models.*
import com.mylinetelecom.softphone.service.SipService
import com.mylinetelecom.softphone.ui.screens.*
import com.mylinetelecom.softphone.ui.theme.MyLineSoftphoneTheme
import android.util.Log
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private var sipServiceState = mutableStateOf<SipService?>(null)
    private var serviceBound = false
    private var pendingAnswer = false
    private var pendingChatNumber = mutableStateOf<String?>(null)
    private var pendingChatType  = mutableStateOf("sms")

    private lateinit var settingsRepo: SettingsRepository
    private lateinit var database: AppDatabase

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as SipService.LocalBinder
            val service = localBinder.getService()
            sipServiceState.value = service
            serviceBound = true
            if (pendingAnswer) {
                pendingAnswer = false
                Log.i("MainActivity", "Answering pending call after service bind")
                service.sipHandler.answerCall()
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            sipServiceState.value = null
            serviceBound = false
        }
    }

    private val requestPermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        if (permissions[Manifest.permission.RECORD_AUDIO] == true) {
            startAndBindService()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        settingsRepo = SettingsRepository(this)
        database = AppDatabase.getInstance(this)

        // Request permissions
        val neededPermissions = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            neededPermissions.add(Manifest.permission.POST_NOTIFICATIONS)
        }

        val hasAll = neededPermissions.all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }

        if (hasAll) {
            startAndBindService()
        } else {
            requestPermissions.launch(neededPermissions.toTypedArray())
        }

        // Handle answer/chat actions from notifications
        handleActionIntent(intent)

        setContent {
            val theme by settingsRepo.theme.collectAsState(initial = "dark")
            val sipService by sipServiceState

            MyLineSoftphoneTheme(darkTheme = theme == "dark") {
                MainApp(
                    settingsRepo = settingsRepo,
                    database = database,
                    sipService = sipService,
                    pendingChatNumber = pendingChatNumber,
                    pendingChatType = pendingChatType,
                    onSaveConfig = { config ->
                        lifecycleScope.launch {
                            settingsRepo.saveSipConfig(config)
                            sipService?.reconfigure(config)
                        }
                    },
                    onThemeChange = { newTheme ->
                        lifecycleScope.launch {
                            settingsRepo.saveTheme(newTheme)
                        }
                    },
                    onToggleSpeaker = { toggleSpeaker() },
                    onExitApp = { exitApp() }
                )
            }
        }
    }

    private fun startAndBindService() {
        val intent = Intent(this, SipService::class.java)
        startForegroundService(intent)
        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
    }

    private fun toggleSpeaker(): Boolean {
        val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
        val newState = !audioManager.isSpeakerphoneOn
        audioManager.isSpeakerphoneOn = newState
        return newState
    }

    private fun exitApp() {
        Log.i("MainActivity", "exitApp called")

        // 1. Unbind from service
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }

        // 2. Stop the foreground service — triggers performCleanShutdown which
        //    handles unregister via stopBlocking() (no need for separate quick unregister)
        val intent = Intent(this, SipService::class.java)
        stopService(intent)

        // 3. Give the unregister packet a moment to go out, then close
        window.decorView.postDelayed({
            finishAndRemoveTask()
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 500)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleActionIntent(intent)
    }

    private fun handleActionIntent(intent: Intent?) {
        when (intent?.action) {
            "ACTION_ANSWER" -> {
                Log.i("MainActivity", "Answer action received from notification")
                sipServiceState.value?.sipHandler?.answerCall()
                    ?: run {
                        pendingAnswer = true
                    }
            }
            "ACTION_OPEN_CHAT" -> {
                val number = intent.getStringExtra("chat_number")
                if (number != null) {
                    Log.i("MainActivity", "Opening chat for $number from notification")
                    pendingChatNumber.value = number
                    pendingChatType.value = intent.getStringExtra("chat_type") ?: "sms"
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        // Rebind to service when returning from background
        if (!serviceBound) {
            val intent = Intent(this, SipService::class.java)
            bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
        }
    }

    override fun onStop() {
        // Unbind when going to background — service keeps running as foreground service
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
            sipServiceState.value = null
        }
        super.onStop()
    }

    override fun onDestroy() {
        // Safety net — unbind if still bound
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }
        super.onDestroy()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainApp(
    settingsRepo: SettingsRepository,
    database: AppDatabase,
    sipService: SipService?,
    pendingChatNumber: MutableState<String?> = mutableStateOf(null),
    pendingChatType: MutableState<String> = mutableStateOf("sms"),
    onSaveConfig: (SipConfig) -> Unit,
    onThemeChange: (String) -> Unit,
    onToggleSpeaker: () -> Boolean,
    onExitApp: () -> Unit = {}
) {
    // State
    var currentScreen by remember { mutableStateOf("dialpad") }
    var dialNumber by remember { mutableStateOf("") }
    var showTransferDialog by remember { mutableStateOf(false) }
    var showInCallDialpad by remember { mutableStateOf(false) }
    var isMuted by remember { mutableStateOf(false) }
    var isSpeaker by remember { mutableStateOf(false) }
    var chatNumber by remember { mutableStateOf<String?>(null) }
    var chatType   by remember { mutableStateOf("sms") }     // "sms" or "whatsapp"
    var showNewMessageDialog by remember { mutableStateOf(false) }
    var showWhatsAppTemplateDialog by remember { mutableStateOf(false) }

    // Handle notification tap — navigate to chat
    LaunchedEffect(pendingChatNumber.value) {
        pendingChatNumber.value?.let { number ->
            currentScreen = "messages"
            chatNumber = number
            chatType   = pendingChatType.value
            pendingChatNumber.value = null
        }
    }

    // When a chat is opened, clear its notification badge
    LaunchedEffect(chatNumber, chatType) {
        chatNumber?.let { sipService?.clearMessageNotification(it) }
    }

    // SIP state - read directly from SipService's Compose-observable state
    val callState = sipService?.composeCallState ?: CallState.IDLE
    val registrationState = sipService?.composeRegistrationState ?: RegistrationState.UNREGISTERED
    val remoteNumber = sipService?.composeRemoteNumber ?: ""
    val remoteName = sipService?.composeRemoteName ?: ""
    val callDuration = sipService?.composeCallDuration ?: 0
    val blfStates = sipService?.composeBlfStates ?: emptyMap()
    val isConsulting = sipService?.composeIsConsulting ?: false
    val consultState = sipService?.composeConsultState ?: CallState.IDLE
    val consultNumber = sipService?.composeConsultNumber ?: ""

    // Data flows
    val callHistory by database.callHistoryDao().getAll().collectAsState(initial = emptyList())
    val contacts by database.contactsDao().getAll().collectAsState(initial = emptyList())
    val smsConversations by database.chatMessageDao().getConversationsForType("sms").collectAsState(initial = emptyList())
    val waConversations  by database.chatMessageDao().getConversationsForType("whatsapp").collectAsState(initial = emptyList())
    val chatMessages by (chatNumber?.let {
        database.chatMessageDao().getMessagesForChat(it, chatType)
    } ?: flowOf(emptyList<ChatMessage>())).collectAsState(initial = emptyList())
    val sipConfig by settingsRepo.sipConfig.collectAsState(initial = SipConfig())
    val theme by settingsRepo.theme.collectAsState(initial = "dark")
    val blfEntries by settingsRepo.blfEntries.collectAsState(initial = emptyList())

    val scope = rememberCoroutineScope()

    // Auto-navigate to InCall screen on active call
    val isInCall = callState != CallState.IDLE && callState != CallState.DISCONNECTED

    // Helper to make a call
    val makeCall: (String) -> Unit = { number ->
        if (number.isNotEmpty()) {
            sipService?.sipHandler?.makeCall(number)
        }
    }

    Scaffold(
        bottomBar = {
            if (!isInCall) {
                NavigationBar {
                    NavigationBarItem(
                        selected = currentScreen == "dialpad",
                        onClick = { currentScreen = "dialpad" },
                        icon = { Icon(Icons.Default.Dialpad, "Dialpad") },
                        label = { Text("Dialpad") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == "contacts",
                        onClick = { currentScreen = "contacts" },
                        icon = { Icon(Icons.Default.Contacts, "Contacts") },
                        label = { Text("Contacts") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == "messages",
                        onClick = { currentScreen = "messages"; chatNumber = null },
                        icon = { Icon(Icons.Default.Chat, "Messages") },
                        label = { Text("Messages") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == "history",
                        onClick = { currentScreen = "history" },
                        icon = { Icon(Icons.Default.History, "History") },
                        label = { Text("History") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == "blf",
                        onClick = { currentScreen = "blf" },
                        icon = { Icon(Icons.Default.Visibility, "BLF") },
                        label = { Text("BLF") }
                    )
                }
            }
        }
    ) { paddingValues ->
        Box(modifier = Modifier.padding(paddingValues)) {
            if (isInCall) {
                // In-call screen
                InCallScreen(
                    callState = callState,
                    remoteNumber = remoteNumber,
                    remoteName = remoteName,
                    callDuration = callDuration,
                    isMuted = isMuted,
                    isSpeaker = isSpeaker,
                    isConsulting = isConsulting,
                    consultState = consultState,
                    consultNumber = consultNumber,
                    onHangup = {
                        sipService?.sipHandler?.hangup()
                        isMuted = false
                        isSpeaker = false
                        showInCallDialpad = false
                    },
                    onAnswer = {
                        sipService?.sipHandler?.answerCall()
                    },
                    onMuteToggle = {
                        val newMuted = sipService?.sipHandler?.toggleMute() ?: false
                        isMuted = newMuted
                    },
                    onHoldToggle = {
                        sipService?.sipHandler?.toggleHold()
                    },
                    onSpeakerToggle = {
                        isSpeaker = onToggleSpeaker()
                    },
                    onTransfer = {
                        showTransferDialog = true
                    },
                    onDigitPress = { digit ->
                        sipService?.sipHandler?.sendDtmf(digit)
                    },
                    showDialpad = showInCallDialpad,
                    onToggleDialpad = {
                        showInCallDialpad = !showInCallDialpad
                    },
                    onCompleteTransfer = {
                        sipService?.sipHandler?.attendedTransferComplete()
                    },
                    onCancelTransfer = {
                        sipService?.sipHandler?.attendedTransferCancel()
                    }
                )
            } else {
                when (currentScreen) {
                    "dialpad" -> DialpadScreen(
                        dialNumber = dialNumber,
                        onDialNumberChange = { dialNumber = it },
                        callState = callState,
                        registrationState = registrationState,
                        onCall = { makeCall(dialNumber) },
                        onHangup = { sipService?.sipHandler?.hangup() },
                        onDigitPress = { digit ->
                            if (callState != CallState.IDLE) {
                                sipService?.sipHandler?.sendDtmf(digit)
                            }
                        },
                        onSettingsClick = { currentScreen = "settings" },
                        onRegisterToggle = {
                            if (registrationState == RegistrationState.REGISTERED) {
                                sipService?.sipHandler?.stop()
                            } else {
                                scope.launch {
                                    val config = settingsRepo.sipConfig.first()
                                    sipService?.reconfigure(config)
                                }
                            }
                        },
                        onExitApp = onExitApp
                    )

                    "contacts" -> ContactsScreen(
                        contacts = contacts,
                        onCall = { number -> 
                            dialNumber = number
                            makeCall(number) 
                        },
                        onAddContact = { name, number ->
                            scope.launch {
                                database.contactsDao().insert(
                                    Contact(name = name, number = number)
                                )
                            }
                        },
                        onEditContact = { contact ->
                            scope.launch {
                                database.contactsDao().update(contact)
                            }
                        },
                        onDeleteContact = { contact ->
                            scope.launch {
                                database.contactsDao().delete(contact)
                            }
                        },
                        onToggleFavorite = { contact ->
                            scope.launch {
                                database.contactsDao().update(
                                    contact.copy(isFavorite = !contact.isFavorite)
                                )
                            }
                        }
                    )

                    "messages" -> {
                        if (chatNumber != null) {
                            val displayName = contacts.find { it.number == chatNumber }?.name
                            ChatDetailScreen(
                                remoteNumber = chatNumber!!,
                                displayName = displayName,
                                messageType = chatType,
                                messages = chatMessages,
                                onSendMessage = { text ->
                                    scope.launch {
                                        val rawNumber = chatNumber!!
                                        // Normalise WhatsApp peer to '+E.164' format so outbound
                                        // DB keys match inbound (server delivers with '+' prefix).
                                        val storedNumber = if (chatType == "whatsapp")
                                            normalizeWhatsAppNumber(rawNumber) else rawNumber

                                        // WhatsApp template guard: Meta blocks free-form messages
                                        // to contacts that have never messaged us first.
                                        // If no inbound messages exist, show the template dialog.
                                        if (chatType == "whatsapp") {
                                            val inboundCount = database.chatMessageDao()
                                                .countInbound(storedNumber, "whatsapp")
                                            if (inboundCount == 0) {
                                                showWhatsAppTemplateDialog = true
                                                return@launch
                                            }
                                        }

                                        // Build the SIP recipient:
                                        //   WhatsApp → 11 digits (leading 1); sms_send.php keeps it
                                        //   SMS      → 10 digits; sms_send.php forces SMS delivery
                                        val sipRecipient = if (chatType == "whatsapp") {
                                            val digits = storedNumber.replace(Regex("[^0-9]"), "")
                                            if (digits.length == 10) "1$digits" else digits
                                        } else {
                                            val digits = rawNumber.replace(Regex("[^0-9]"), "")
                                            if (digits.length == 11 && digits.startsWith("1")) digits.substring(1) else digits
                                        }
                                        val msg = ChatMessage(
                                            remoteNumber = storedNumber,   // normalised key
                                            body = text,
                                            isOutgoing = true,
                                            status = MessageStatus.SENT,
                                            messageType = chatType
                                        )
                                        database.chatMessageDao().insert(msg)
                                        // Pass channel so SipHandler sends X-Channel header
                                        sipService?.sipHandler?.sendMessage(sipRecipient, text, chatType)
                                    }
                                },
                                onBack = { chatNumber = null },
                                onCall = { number ->
                                    dialNumber = number
                                    makeCall(number)
                                }
                            )
                        } else {
                            MessagesScreen(
                                smsConversations = smsConversations,
                                waConversations = waConversations,
                                contacts = contacts,
                                onOpenChat = { number, type ->
                                    chatNumber = number
                                    chatType = type
                                },
                                onNewMessage = { showNewMessageDialog = true },
                                onDeleteConversation = { number, type ->
                                    scope.launch {
                                        database.chatMessageDao().deleteConversation(number, type)
                                    }
                                },
                                onClearAll = { type ->
                                    scope.launch {
                                        database.chatMessageDao().deleteByType(type)
                                    }
                                }
                            )
                        }
                    }

                    "history" -> CallHistoryScreen(
                        callHistory = callHistory,
                        onCall = { number -> 
                            dialNumber = number
                            makeCall(number) 
                        },
                        onDelete = { record ->
                            scope.launch {
                                database.callHistoryDao().delete(record)
                            }
                        },
                        onDeleteAll = {
                            scope.launch {
                                database.callHistoryDao().deleteAll()
                            }
                        }
                    )

                    "blf" -> BlfScreen(
                        entries = blfEntries,
                        blfStates = blfStates,
                        onCall = { ext ->
                            android.util.Log.i("MainApp", "BLF call tapped: $ext, sipService=${sipService != null}")
                            dialNumber = ext
                            makeCall(ext)
                        },
                        onAddEntry = { ext, label ->
                            scope.launch {
                                val newEntries = blfEntries + BlfEntry(extension = ext, label = label)
                                settingsRepo.saveBlfEntries(newEntries)
                                sipService?.sipHandler?.subscribeBlfExtension(ext)
                            }
                        },
                        onRemoveEntry = { entry ->
                            scope.launch {
                                val newEntries = blfEntries.filter { it.extension != entry.extension }
                                settingsRepo.saveBlfEntries(newEntries)
                                sipService?.sipHandler?.unsubscribeBlfExtension(entry.extension)
                            }
                        }
                    )

                    "settings" -> SettingsScreen(
                        currentConfig = sipConfig,
                        currentTheme = theme,
                        onSave = onSaveConfig,
                        onThemeChange = onThemeChange,
                        onBack = { currentScreen = "dialpad" }
                    )
                }
            }
        }
    }

    // Transfer dialog
    if (showTransferDialog) {
        TransferDialog(
            onDismiss = { showTransferDialog = false },
            onBlindTransfer = { target ->
                sipService?.sipHandler?.blindTransfer(target)
            },
            onAttendedTransfer = { target ->
                sipService?.sipHandler?.attendedTransferStart(target)
            }
        )
    }

    // WhatsApp template dialog — shown when user tries to start a new WhatsApp
    // conversation with a contact who has never messaged us (Meta error 131047).
    if (showWhatsAppTemplateDialog) {
        val TEMPLATE_NAME = "initial_contact"
        val TEMPLATE_LANG = "en"
        val TEMPLATE_DISPLAY = "We would like to connect with you. " +
                "Please reply to this message so we can assist you."
        AlertDialog(
            onDismissRequest = { showWhatsAppTemplateDialog = false },
            title = { Text("WhatsApp — new conversation") },
            text = {
                Text(
                    "${chatNumber ?: "This contact"} has not messaged you on WhatsApp yet.\n\n" +
                    "Meta will not deliver a free-form message to start a new conversation. " +
                    "Send the pre-approved template instead?\n\n" +
                    "The template reads:\n\"$TEMPLATE_DISPLAY\"\n\n" +
                    "Once they reply you can send any message normally."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showWhatsAppTemplateDialog = false
                    scope.launch {
                        val rawNumber = chatNumber ?: return@launch
                        val storedNumber = normalizeWhatsAppNumber(rawNumber)
                        val digits = storedNumber.replace(Regex("[^0-9]"), "")
                        val sipRecipient = if (digits.length == 10) "1$digits" else digits
                        val templateBody = "__TEMPLATE__:${TEMPLATE_NAME}:${TEMPLATE_LANG}"
                        val msg = ChatMessage(
                            remoteNumber = storedNumber,
                            body = "[Template] $TEMPLATE_DISPLAY",
                            isOutgoing = true,
                            status = MessageStatus.SENT,
                            messageType = "whatsapp"
                        )
                        database.chatMessageDao().insert(msg)
                        sipService?.sipHandler?.sendMessage(sipRecipient, templateBody, "whatsapp")
                    }
                }) { Text("Send Template") }
            },
            dismissButton = {
                TextButton(onClick = { showWhatsAppTemplateDialog = false }) { Text("Cancel") }
            }
        )
    }

    // New message dialog
    if (showNewMessageDialog) {
        NewMessageDialog(
            onDismiss = { showNewMessageDialog = false },
            onStartChat = { number, type ->
                showNewMessageDialog = false
                // Normalise WhatsApp numbers immediately so the first message
                // is stored under the same '+E.164' key as any later inbound reply.
                chatNumber = if (type == "whatsapp") normalizeWhatsAppNumber(number) else number
                chatType = type
                currentScreen = "messages"
            }
        )
    }
}

@Composable
private fun NewMessageDialog(
    onDismiss: () -> Unit,
    onStartChat: (number: String, type: String) -> Unit
) {
    var number by remember { mutableStateOf("") }
    var selectedType by remember { mutableStateOf("sms") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New Message") },
        text = {
            Column {
                // Channel selector
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    FilterChip(
                        selected = selectedType == "sms",
                        onClick = { selectedType = "sms" },
                        label = { Text("SMS") },
                        leadingIcon = {
                            Icon(Icons.Default.Sms, null, modifier = Modifier.size(16.dp))
                        },
                        modifier = Modifier.weight(1f)
                    )
                    FilterChip(
                        selected = selectedType == "whatsapp",
                        onClick = { selectedType = "whatsapp" },
                        label = { Text("WhatsApp") },
                        leadingIcon = {
                            Icon(Icons.Default.Chat, null, modifier = Modifier.size(16.dp))
                        },
                        modifier = Modifier.weight(1f)
                    )
                }
                Spacer(modifier = Modifier.height(12.dp))
                OutlinedTextField(
                    value = number,
                    onValueChange = { number = it },
                    label = { Text("Phone number") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onStartChat(number.trim(), selectedType) },
                enabled = number.isNotBlank()
            ) {
                Text("Start")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        }
    )
}

/**
 * Normalise a phone number to '+E.164' format for WhatsApp storage.
 *
 * The FusionPBX/FreeSWITCH server delivers inbound WhatsApp messages with the
 * From number in '+E.164' form (e.g. +13059684280).  Storing outbound messages
 * under the same format ensures sent and received messages share one DB key and
 * appear together in a single conversation thread.
 */
private fun normalizeWhatsAppNumber(number: String): String {
    if (number.startsWith("+")) return number          // already normalised
    val digits = number.replace(Regex("[^0-9]"), "")
    return when {
        digits.length == 10 -> "+1$digits"             // US 10-digit → +1XXXXXXXXXX
        digits.length == 11 && digits.startsWith("1") -> "+$digits"  // US 11-digit
        else -> "+$digits"                             // international: just add '+'
    }
}
