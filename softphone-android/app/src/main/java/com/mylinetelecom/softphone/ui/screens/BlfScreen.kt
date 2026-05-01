package com.mylinetelecom.softphone.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.BlfEntry
import com.mylinetelecom.softphone.models.BlfState
import com.mylinetelecom.softphone.ui.theme.*

@Composable
fun BlfScreen(
    entries: List<BlfEntry>,
    blfStates: Map<String, BlfState>,
    onCall: (String) -> Unit,
    onAddEntry: (String, String) -> Unit,
    onRemoveEntry: (BlfEntry) -> Unit
) {
    var showAddDialog by remember { mutableStateOf(false) }

    Column(modifier = Modifier.fillMaxSize()) {
        // Header
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("BLF Monitor", fontSize = 24.sp, color = MaterialTheme.colorScheme.onSurface)
            IconButton(onClick = { showAddDialog = true }) {
                Icon(Icons.Default.Add, "Add extension")
            }
        }

        if (entries.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        "No extensions monitored",
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        "Tap + to add an extension",
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp)
            ) {
                items(entries) { entry ->
                    val state = blfStates[entry.extension] ?: BlfState.UNKNOWN

                    BlfItem(
                        entry = entry,
                        state = state,
                        onCall = { onCall(entry.extension) },
                        onRemove = { onRemoveEntry(entry) }
                    )
                }
            }
        }
    }

    if (showAddDialog) {
        BlfAddDialog(
            onDismiss = { showAddDialog = false },
            onAdd = { ext, label ->
                onAddEntry(ext, label)
                showAddDialog = false
            }
        )
    }
}

@Composable
private fun BlfItem(
    entry: BlfEntry,
    state: BlfState,
    onCall: () -> Unit,
    onRemove: () -> Unit
) {
    val stateColor = when (state) {
        BlfState.IDLE -> BlfIdle
        BlfState.RINGING -> BlfRinging
        BlfState.BUSY -> BlfBusy
        BlfState.UNKNOWN -> BlfUnknown
        BlfState.OFFLINE -> BlfOffline
    }

    val stateText = when (state) {
        BlfState.IDLE -> "Available"
        BlfState.RINGING -> "Ringing"
        BlfState.BUSY -> "Busy"
        BlfState.UNKNOWN -> "Unknown"
        BlfState.OFFLINE -> "Offline"
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
            // Status indicator
            Surface(
                modifier = Modifier.size(14.dp),
                shape = CircleShape,
                color = stateColor
            ) {}

            Spacer(modifier = Modifier.width(12.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    entry.displayName,
                    fontSize = 16.sp,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Row {
                    if (entry.label.isNotBlank()) {
                        Text(
                            "Ext. ${entry.extension}",
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
                        stateText,
                        fontSize = 13.sp,
                        color = stateColor
                    )
                }
            }

            IconButton(onClick = onCall) {
                Icon(
                    Icons.Default.Call,
                    contentDescription = "Call",
                    tint = CallGreen,
                    modifier = Modifier.size(20.dp)
                )
            }

            IconButton(onClick = onRemove) {
                Icon(
                    Icons.Default.RemoveCircleOutline,
                    contentDescription = "Remove",
                    tint = CallRed,
                    modifier = Modifier.size(20.dp)
                )
            }
        }
    }
}

@Composable
private fun BlfAddDialog(
    onDismiss: () -> Unit,
    onAdd: (extension: String, label: String) -> Unit
) {
    var extension by remember { mutableStateOf("") }
    var label by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Monitor Extension") },
        text = {
            Column {
                OutlinedTextField(
                    value = extension,
                    onValueChange = { extension = it },
                    label = { Text("Extension") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(modifier = Modifier.height(8.dp))
                OutlinedTextField(
                    value = label,
                    onValueChange = { label = it },
                    label = { Text("Label (optional)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onAdd(extension, label) },
                enabled = extension.isNotBlank()
            ) {
                Text("Add")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        }
    )
}
