package com.mylinetelecom.softphone.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.CallState
import com.mylinetelecom.softphone.ui.theme.*

@Composable
fun InCallScreen(
    callState: CallState,
    remoteNumber: String,
    remoteName: String,
    callDuration: Int,
    isMuted: Boolean,
    isSpeaker: Boolean,
    isConsulting: Boolean = false,
    consultState: CallState = CallState.IDLE,
    consultNumber: String = "",
    onHangup: () -> Unit,
    onAnswer: () -> Unit,
    onMuteToggle: () -> Unit,
    onHoldToggle: () -> Unit,
    onSpeakerToggle: () -> Unit,
    onTransfer: () -> Unit,
    onDigitPress: (Char) -> Unit,
    showDialpad: Boolean,
    onToggleDialpad: () -> Unit,
    onCompleteTransfer: () -> Unit = {},
    onCancelTransfer: () -> Unit = {}
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Spacer(modifier = Modifier.height(48.dp))

        // Caller info
        Surface(
            modifier = Modifier.size(80.dp),
            shape = CircleShape,
            color = MaterialTheme.colorScheme.surfaceVariant
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    Icons.Default.Person,
                    contentDescription = null,
                    modifier = Modifier.size(48.dp),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        Text(
            text = remoteName.ifBlank { remoteNumber },
            fontSize = 28.sp,
            fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.onSurface
        )

        if (remoteName.isNotBlank()) {
            Text(
                text = remoteNumber,
                fontSize = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        Spacer(modifier = Modifier.height(8.dp))

        // Status / Timer
        Text(
            text = when {
                isConsulting && callState == CallState.HOLD -> "On Hold - ${formatDuration(callDuration)}"
                callState == CallState.CALLING -> "Calling..."
                callState == CallState.RINGING -> "Ringing..."
                callState == CallState.INCOMING -> "Incoming Call"
                callState == CallState.CONFIRMED -> formatDuration(callDuration)
                callState == CallState.HOLD -> "On Hold - ${formatDuration(callDuration)}"
                else -> ""
            },
            fontSize = 16.sp,
            color = when (callState) {
                CallState.HOLD -> HoldColor
                CallState.INCOMING -> CallGreen
                else -> MaterialTheme.colorScheme.onSurfaceVariant
            }
        )

        // Attended transfer consultation info
        if (isConsulting) {
            Spacer(modifier = Modifier.height(16.dp))

            Surface(
                modifier = Modifier.fillMaxWidth(),
                color = MaterialTheme.colorScheme.secondaryContainer,
                shape = MaterialTheme.shapes.medium
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Text(
                        text = "Attended Transfer",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = when (consultState) {
                            CallState.CALLING -> "Calling $consultNumber..."
                            CallState.RINGING -> "Ringing $consultNumber..."
                            CallState.CONFIRMED -> "Connected to $consultNumber"
                            else -> "Consultation: $consultNumber"
                        },
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceEvenly
                    ) {
                        Button(
                            onClick = onCompleteTransfer,
                            enabled = consultState == CallState.CONFIRMED,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = CallGreen
                            )
                        ) {
                            Text("Complete", color = Color.White)
                        }
                        Button(
                            onClick = onCancelTransfer,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = CallRed
                            )
                        ) {
                            Text("Cancel", color = Color.White)
                        }
                    }
                }
            }
        }

        Spacer(modifier = Modifier.weight(1f))

        // In-call DTMF dialpad
        if (showDialpad && callState == CallState.CONFIRMED) {
            InCallDialpad(onDigitPress)
            Spacer(modifier = Modifier.height(16.dp))
        }

        // Mid-call controls (only when connected)
        if (callState == CallState.CONFIRMED || callState == CallState.HOLD) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                // Mute
                CallControlButton(
                    icon = if (isMuted) Icons.Default.MicOff else Icons.Default.Mic,
                    label = if (isMuted) "Unmute" else "Mute",
                    isActive = isMuted,
                    activeColor = MuteColor,
                    onClick = onMuteToggle
                )

                // Hold
                CallControlButton(
                    icon = if (callState == CallState.HOLD) Icons.Default.PlayArrow else Icons.Default.Pause,
                    label = if (callState == CallState.HOLD) "Resume" else "Hold",
                    isActive = callState == CallState.HOLD,
                    activeColor = HoldColor,
                    onClick = onHoldToggle
                )

                // Speaker
                CallControlButton(
                    icon = if (isSpeaker) Icons.Default.VolumeUp else Icons.Default.VolumeDown,
                    label = "Speaker",
                    isActive = isSpeaker,
                    activeColor = BrandPrimary,
                    onClick = onSpeakerToggle
                )

                // Dialpad toggle
                CallControlButton(
                    icon = Icons.Default.Dialpad,
                    label = "Keypad",
                    isActive = showDialpad,
                    activeColor = BrandPrimary,
                    onClick = onToggleDialpad
                )

                // Transfer
                CallControlButton(
                    icon = Icons.Default.CallSplit,
                    label = "Transfer",
                    onClick = onTransfer
                )
            }

            Spacer(modifier = Modifier.height(32.dp))
        }

        // Answer / Hangup buttons
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            if (callState == CallState.INCOMING) {
                // Answer button
                FloatingActionButton(
                    onClick = onAnswer,
                    containerColor = CallGreen,
                    shape = CircleShape,
                    modifier = Modifier.size(64.dp)
                ) {
                    Icon(
                        Icons.Default.Call,
                        contentDescription = "Answer",
                        tint = Color.White,
                        modifier = Modifier.size(32.dp)
                    )
                }
            }

            // Hangup button
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

        Spacer(modifier = Modifier.height(32.dp))
    }
}

@Composable
private fun CallControlButton(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    label: String,
    isActive: Boolean = false,
    activeColor: Color = BrandPrimary,
    onClick: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        IconButton(
            onClick = onClick,
            modifier = Modifier.size(48.dp)
        ) {
            Surface(
                modifier = Modifier.size(48.dp),
                shape = CircleShape,
                color = if (isActive) activeColor.copy(alpha = 0.2f)
                else MaterialTheme.colorScheme.surfaceVariant
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Icon(
                        icon,
                        contentDescription = label,
                        tint = if (isActive) activeColor
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(24.dp)
                    )
                }
            }
        }
        Spacer(modifier = Modifier.height(4.dp))
        Text(
            text = label,
            fontSize = 11.sp,
            color = if (isActive) activeColor
            else MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
private fun InCallDialpad(onDigitPress: (Char) -> Unit) {
    val digits = listOf(
        '1', '2', '3',
        '4', '5', '6',
        '7', '8', '9',
        '*', '0', '#'
    )

    Column {
        for (row in digits.chunked(3)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                for (digit in row) {
                    TextButton(onClick = { onDigitPress(digit) }) {
                        Text(
                            digit.toString(),
                            fontSize = 24.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }
            }
        }
    }
}

private fun formatDuration(seconds: Int): String {
    val h = seconds / 3600
    val m = (seconds % 3600) / 60
    val s = seconds % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s)
    else "%02d:%02d".format(m, s)
}
