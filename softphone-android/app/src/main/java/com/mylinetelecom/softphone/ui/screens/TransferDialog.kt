package com.mylinetelecom.softphone.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun TransferDialog(
    onDismiss: () -> Unit,
    onBlindTransfer: (String) -> Unit,
    onAttendedTransfer: (String) -> Unit
) {
    var targetNumber by remember { mutableStateOf("") }
    var transferType by remember { mutableStateOf("blind") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Transfer Call") },
        text = {
            Column {
                OutlinedTextField(
                    value = targetNumber,
                    onValueChange = { targetNumber = it },
                    label = { Text("Transfer to") },
                    placeholder = { Text("Enter number or extension") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )

                Spacer(modifier = Modifier.height(16.dp))

                Text("Transfer Type", style = MaterialTheme.typography.labelMedium)
                Spacer(modifier = Modifier.height(4.dp))

                Row {
                    Row(modifier = Modifier.weight(1f)) {
                        RadioButton(
                            selected = transferType == "blind",
                            onClick = { transferType = "blind" }
                        )
                        Text("Blind", modifier = Modifier.padding(top = 12.dp))
                    }
                    Row(modifier = Modifier.weight(1f)) {
                        RadioButton(
                            selected = transferType == "attended",
                            onClick = { transferType = "attended" }
                        )
                        Text("Attended", modifier = Modifier.padding(top = 12.dp))
                    }
                }
            }
        },
        confirmButton = {
            TextButton(
                onClick = {
                    if (targetNumber.isNotBlank()) {
                        when (transferType) {
                            "blind" -> onBlindTransfer(targetNumber)
                            "attended" -> onAttendedTransfer(targetNumber)
                        }
                        onDismiss()
                    }
                },
                enabled = targetNumber.isNotBlank()
            ) {
                Text("Transfer")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        }
    )
}
