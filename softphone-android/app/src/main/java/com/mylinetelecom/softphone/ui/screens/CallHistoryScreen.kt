package com.mylinetelecom.softphone.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.CallDirection
import com.mylinetelecom.softphone.models.CallRecord
import com.mylinetelecom.softphone.ui.theme.*
import java.text.SimpleDateFormat
import java.util.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CallHistoryScreen(
    callHistory: List<CallRecord>,
    onCall: (String) -> Unit,
    onDelete: (CallRecord) -> Unit,
    onDeleteAll: () -> Unit
) {
    var filter by remember { mutableStateOf("all") } // all, inbound, outbound

    val filteredHistory = callHistory.filter { record ->
        when (filter) {
            "inbound" -> record.direction == CallDirection.INBOUND
            "outbound" -> record.direction == CallDirection.OUTBOUND
            else -> true
        }
    }

    Column(modifier = Modifier.fillMaxSize()) {
        // Header
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Call History", fontSize = 24.sp, color = MaterialTheme.colorScheme.onSurface)
            if (callHistory.isNotEmpty()) {
                TextButton(onClick = onDeleteAll) {
                    Text("Clear All", color = CallRed, fontSize = 12.sp)
                }
            }
        }

        // Filter tabs
        Row(modifier = Modifier.padding(horizontal = 16.dp)) {
            FilterChip(
                selected = filter == "all",
                onClick = { filter = "all" },
                label = { Text("All") }
            )
            Spacer(modifier = Modifier.width(8.dp))
            FilterChip(
                selected = filter == "inbound",
                onClick = { filter = "inbound" },
                label = { Text("Received") },
                leadingIcon = if (filter == "inbound") {
                    { Icon(Icons.Default.CallReceived, null, modifier = Modifier.size(16.dp)) }
                } else null
            )
            Spacer(modifier = Modifier.width(8.dp))
            FilterChip(
                selected = filter == "outbound",
                onClick = { filter = "outbound" },
                label = { Text("Dialed") },
                leadingIcon = if (filter == "outbound") {
                    { Icon(Icons.Default.CallMade, null, modifier = Modifier.size(16.dp)) }
                } else null
            )
        }

        Spacer(modifier = Modifier.height(8.dp))

        // History list
        if (filteredHistory.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    "No call history",
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp)
            ) {
                items(filteredHistory, key = { it.id }) { record ->
                    CallHistoryItem(
                        record = record,
                        onCall = { onCall(record.remoteNumber) },
                        onDelete = { onDelete(record) }
                    )
                }
            }
        }
    }
}

@Composable
private fun CallHistoryItem(
    record: CallRecord,
    onCall: () -> Unit,
    onDelete: () -> Unit
) {
    var showMenu by remember { mutableStateOf(false) }

    val directionIcon = when (record.direction) {
        CallDirection.OUTBOUND -> Icons.Default.CallMade
        CallDirection.INBOUND -> Icons.Default.CallReceived
    }

    val statusColor = when (record.status) {
        "answered", "ended" -> CallGreen
        "missed", "rejected" -> CallRed
        "busy" -> CallOrange
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .clickable(onClick = onCall),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Direction icon
            Icon(
                directionIcon,
                contentDescription = record.direction.name,
                tint = statusColor,
                modifier = Modifier.size(24.dp)
            )

            Spacer(modifier = Modifier.width(12.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    record.remoteName.ifBlank { record.remoteNumber },
                    fontSize = 16.sp,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Row {
                    if (record.remoteName.isNotBlank()) {
                        Text(
                            record.remoteNumber,
                            fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Text(
                            " - ",
                            fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Text(
                        formatTimestamp(record.startedAt),
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                if (record.durationSeconds > 0) {
                    Text(
                        formatCallDuration(record.durationSeconds),
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }

            // Call button
            IconButton(onClick = onCall) {
                Icon(
                    Icons.Default.Call,
                    contentDescription = "Call back",
                    tint = CallGreen,
                    modifier = Modifier.size(20.dp)
                )
            }

            // Delete
            Box {
                IconButton(onClick = { showMenu = true }) {
                    Icon(
                        Icons.Default.MoreVert,
                        contentDescription = "More",
                        modifier = Modifier.size(20.dp)
                    )
                }
                DropdownMenu(
                    expanded = showMenu,
                    onDismissRequest = { showMenu = false }
                ) {
                    DropdownMenuItem(
                        text = { Text("Delete") },
                        onClick = { showMenu = false; onDelete() },
                        leadingIcon = { Icon(Icons.Default.Delete, null) }
                    )
                }
            }
        }
    }
}

private fun formatTimestamp(timestamp: Long): String {
    val sdf = SimpleDateFormat("MMM d, HH:mm", Locale.getDefault())
    return sdf.format(Date(timestamp))
}

private fun formatCallDuration(seconds: Int): String {
    val m = seconds / 60
    val s = seconds % 60
    return if (m > 0) "${m}m ${s}s" else "${s}s"
}
