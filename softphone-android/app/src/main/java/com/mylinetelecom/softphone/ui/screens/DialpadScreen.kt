package com.mylinetelecom.softphone.ui.screens

import android.media.ToneGenerator
import android.media.AudioManager
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.CallState
import com.mylinetelecom.softphone.models.RegistrationState
import com.mylinetelecom.softphone.ui.components.DialpadButton
import com.mylinetelecom.softphone.ui.theme.*

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun DialpadScreen(
    dialNumber: String,
    onDialNumberChange: (String) -> Unit,
    callState: CallState,
    registrationState: RegistrationState,
    onCall: () -> Unit,
    onHangup: () -> Unit,
    onDigitPress: (Char) -> Unit,
    onSettingsClick: () -> Unit,
    onRegisterToggle: () -> Unit,
    onExitApp: () -> Unit = {}
) {
    val clipboardManager = LocalClipboardManager.current
    var showCopiedSnackbar by remember { mutableStateOf(false) }

    // DTMF tone generator for dialpad feedback
    val toneGenerator = remember {
        try {
            ToneGenerator(AudioManager.STREAM_DTMF, 80) // 80% volume
        } catch (_: Exception) { null }
    }
    DisposableEffect(Unit) {
        onDispose { toneGenerator?.release() }
    }
    val playTone: (Char) -> Unit = { digit ->
        val tone = when (digit) {
            '0' -> ToneGenerator.TONE_DTMF_0
            '1' -> ToneGenerator.TONE_DTMF_1
            '2' -> ToneGenerator.TONE_DTMF_2
            '3' -> ToneGenerator.TONE_DTMF_3
            '4' -> ToneGenerator.TONE_DTMF_4
            '5' -> ToneGenerator.TONE_DTMF_5
            '6' -> ToneGenerator.TONE_DTMF_6
            '7' -> ToneGenerator.TONE_DTMF_7
            '8' -> ToneGenerator.TONE_DTMF_8
            '9' -> ToneGenerator.TONE_DTMF_9
            '*' -> ToneGenerator.TONE_DTMF_S
            '#' -> ToneGenerator.TONE_DTMF_P
            else -> -1
        }
        if (tone != -1) toneGenerator?.startTone(tone, 150)
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        // Top bar with registration status
        TopBar(
            registrationState = registrationState,
            onSettingsClick = onSettingsClick,
            onRegisterToggle = onRegisterToggle,
            onExitApp = onExitApp
        )

        Spacer(modifier = Modifier.height(24.dp))

        // Number display - long press to copy
        Text(
            text = dialNumber.ifEmpty { "Enter number" },
            fontSize = if (dialNumber.length > 12) 24.sp else 32.sp,
            fontWeight = FontWeight.Light,
            color = if (dialNumber.isEmpty())
                MaterialTheme.colorScheme.onSurfaceVariant
            else
                MaterialTheme.colorScheme.onSurface,
            textAlign = TextAlign.Center,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .height(48.dp)
                .combinedClickable(
                    onClick = {},
                    onLongClick = {
                        if (dialNumber.isNotEmpty()) {
                            clipboardManager.setText(AnnotatedString(dialNumber))
                            showCopiedSnackbar = true
                        }
                    }
                ),
            maxLines = 1
        )

        // Paste / Copy / Backspace buttons
        if (callState == CallState.IDLE) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Paste button
                IconButton(
                    onClick = {
                        val clipText = clipboardManager.getText()?.text ?: ""
                        // Filter to only valid dial characters
                        val filtered = clipText.filter { it.isDigit() || it == '+' || it == '*' || it == '#' }
                        if (filtered.isNotEmpty()) {
                            onDialNumberChange(dialNumber + filtered)
                        }
                    }
                ) {
                    Icon(
                        Icons.Default.ContentPaste,
                        contentDescription = "Paste",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }

                // Copy button (only when there's a number)
                if (dialNumber.isNotEmpty()) {
                    IconButton(
                        onClick = {
                            clipboardManager.setText(AnnotatedString(dialNumber))
                            showCopiedSnackbar = true
                        }
                    ) {
                        Icon(
                            Icons.Default.ContentCopy,
                            contentDescription = "Copy",
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    // Backspace
                    IconButton(
                        onClick = {
                            onDialNumberChange(dialNumber.dropLast(1))
                        }
                    ) {
                        Icon(
                            Icons.Default.Backspace,
                            contentDescription = "Backspace",
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        } else {
            Spacer(modifier = Modifier.height(48.dp))
        }

        // "Copied" notification
        if (showCopiedSnackbar) {
            LaunchedEffect(Unit) {
                kotlinx.coroutines.delay(1500)
                showCopiedSnackbar = false
            }
            Text(
                text = "Number copied",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.primary
            )
        }

        Spacer(modifier = Modifier.height(16.dp))

        // Dialpad grid
        val buttons = listOf(
            Triple("1", "", '1'), Triple("2", "ABC", '2'), Triple("3", "DEF", '3'),
            Triple("4", "GHI", '4'), Triple("5", "JKL", '5'), Triple("6", "MNO", '6'),
            Triple("7", "PQRS", '7'), Triple("8", "TUV", '8'), Triple("9", "WXYZ", '9'),
            Triple("*", "", '*'), Triple("0", "+", '0'), Triple("#", "", '#')
        )

        for (row in buttons.chunked(3)) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(24.dp, Alignment.CenterHorizontally),
                modifier = Modifier.fillMaxWidth()
            ) {
                for ((digit, letters, char) in row) {
                    DialpadButton(
                        digit = digit,
                        letters = letters,
                        onClick = {
                            playTone(char)
                            onDigitPress(char)
                            if (callState == CallState.IDLE) {
                                onDialNumberChange(dialNumber + char)
                            }
                        }
                    )
                }
            }
            Spacer(modifier = Modifier.height(12.dp))
        }

        Spacer(modifier = Modifier.height(20.dp))

        // Call / Hangup button
        when (callState) {
            CallState.IDLE -> {
                FloatingActionButton(
                    onClick = {
                        if (dialNumber.isNotEmpty()) onCall()
                    },
                    containerColor = CallGreen,
                    shape = CircleShape,
                    modifier = Modifier.size(64.dp)
                ) {
                    Icon(
                        Icons.Default.Call,
                        contentDescription = "Call",
                        tint = Color.White,
                        modifier = Modifier.size(32.dp)
                    )
                }
            }
            else -> {
                FloatingActionButton(
                    onClick = onHangup,
                    containerColor = CallRed,
                    shape = CircleShape,
                    modifier = Modifier.size(64.dp)
                ) {
                    Icon(
                        Icons.Default.CallEnd,
                        contentDescription = "Hang up",
                        tint = Color.White,
                        modifier = Modifier.size(32.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun TopBar(
    registrationState: RegistrationState,
    onSettingsClick: () -> Unit,
    onRegisterToggle: () -> Unit,
    onExitApp: () -> Unit
) {
    var showExitDialog by remember { mutableStateOf(false) }

    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Registration status
        Row(verticalAlignment = Alignment.CenterVertically) {
            val statusColor = when (registrationState) {
                RegistrationState.REGISTERED -> CallGreen
                RegistrationState.REGISTERING -> CallOrange
                RegistrationState.FAILED -> CallRed
                RegistrationState.UNREGISTERED -> BlfOffline
            }
            Surface(
                modifier = Modifier.size(12.dp),
                shape = CircleShape,
                color = statusColor
            ) {}

            Spacer(modifier = Modifier.width(8.dp))

            Text(
                text = when (registrationState) {
                    RegistrationState.REGISTERED -> "SIP Registered"
                    RegistrationState.REGISTERING -> "Registering..."
                    RegistrationState.FAILED -> "Registration Failed"
                    RegistrationState.UNREGISTERED -> "Not Registered"
                },
                fontSize = 14.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        // Buttons
        Row {
            TextButton(onClick = onRegisterToggle) {
                Text(
                    if (registrationState == RegistrationState.REGISTERED) "Unregister" else "Register",
                    fontSize = 12.sp
                )
            }
            IconButton(onClick = onSettingsClick) {
                Icon(
                    Icons.Default.Settings,
                    contentDescription = "Settings",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            IconButton(onClick = { showExitDialog = true }) {
                Icon(
                    Icons.Default.ExitToApp,
                    contentDescription = "Exit",
                    tint = CallRed
                )
            }
        }
    }

    // Exit confirmation dialog
    if (showExitDialog) {
        AlertDialog(
            onDismissRequest = { showExitDialog = false },
            title = { Text("Exit App") },
            text = { Text("This will unregister from SIP and close the app. Are you sure?") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showExitDialog = false
                        onExitApp()
                    }
                ) {
                    Text("Exit", color = CallRed)
                }
            },
            dismissButton = {
                TextButton(onClick = { showExitDialog = false }) {
                    Text("Cancel")
                }
            }
        )
    }
}
