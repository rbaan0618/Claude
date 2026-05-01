package com.mylinetelecom.softphone.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.SipConfig

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    currentConfig: SipConfig,
    currentTheme: String,
    onSave: (SipConfig) -> Unit,
    onThemeChange: (String) -> Unit,
    onBack: () -> Unit
) {
    var server by remember { mutableStateOf(currentConfig.server) }
    var port by remember { mutableStateOf(currentConfig.port.toString()) }
    var localPort by remember { mutableStateOf(currentConfig.localPort.toString()) }
    var rport by remember { mutableStateOf(currentConfig.rport) }
    var username by remember { mutableStateOf(currentConfig.username) }
    var password by remember { mutableStateOf(currentConfig.password) }
    var displayName by remember { mutableStateOf(currentConfig.displayName) }
    var transport by remember { mutableStateOf(currentConfig.transport) }
    var enabled by remember { mutableStateOf(currentConfig.enabled) }
    var theme by remember { mutableStateOf(currentTheme) }

    var selectedTab by remember { mutableIntStateOf(0) }
    val tabs = listOf("SIP", "Audio", "General")

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, "Back")
                    }
                },
                actions = {
                    TextButton(onClick = {
                        val config = SipConfig(
                            server = server,
                            port = port.toIntOrNull() ?: 5060,
                            localPort = localPort.toIntOrNull() ?: 5060,
                            rport = rport,
                            username = username,
                            password = password,
                            displayName = displayName,
                            transport = transport,
                            enabled = enabled
                        )
                        onSave(config)
                        onBack()
                    }) {
                        Text("Save")
                    }
                }
            )
        }
    ) { paddingValues ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(paddingValues)
        ) {
            TabRow(selectedTabIndex = selectedTab) {
                tabs.forEachIndexed { index, title ->
                    Tab(
                        selected = selectedTab == index,
                        onClick = { selectedTab = index },
                        text = { Text(title) }
                    )
                }
            }

            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp)
            ) {
                when (selectedTab) {
                    0 -> SipSettings(
                        server = server,
                        onServerChange = { server = it },
                        port = port,
                        onPortChange = { port = it },
                        localPort = localPort,
                        onLocalPortChange = { localPort = it },
                        rport = rport,
                        onRportChange = { rport = it },
                        username = username,
                        onUsernameChange = { username = it },
                        password = password,
                        onPasswordChange = { password = it },
                        displayName = displayName,
                        onDisplayNameChange = { displayName = it },
                        transport = transport,
                        onTransportChange = { transport = it },
                        enabled = enabled,
                        onEnabledChange = { enabled = it }
                    )
                    1 -> AudioSettings()
                    2 -> GeneralSettings(
                        theme = theme,
                        onThemeChange = {
                            theme = it
                            onThemeChange(it)
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun SipSettings(
    server: String, onServerChange: (String) -> Unit,
    port: String, onPortChange: (String) -> Unit,
    localPort: String, onLocalPortChange: (String) -> Unit,
    rport: Boolean, onRportChange: (Boolean) -> Unit,
    username: String, onUsernameChange: (String) -> Unit,
    password: String, onPasswordChange: (String) -> Unit,
    displayName: String, onDisplayNameChange: (String) -> Unit,
    transport: String, onTransportChange: (String) -> Unit,
    enabled: Boolean, onEnabledChange: (Boolean) -> Unit
) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Enable SIP", fontSize = 16.sp)
            Switch(checked = enabled, onCheckedChange = onEnabledChange)
        }

        HorizontalDivider()

        OutlinedTextField(
            value = server,
            onValueChange = onServerChange,
            label = { Text("Domain") },
            placeholder = { Text("e.g. mycompany") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            suffix = { Text(".myline.tel", color = MaterialTheme.colorScheme.onSurfaceVariant) }
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = port,
                onValueChange = onPortChange,
                label = { Text("Port") },
                singleLine = true,
                modifier = Modifier.weight(1f)
            )
            OutlinedTextField(
                value = localPort,
                onValueChange = onLocalPortChange,
                label = { Text("Local Port") },
                singleLine = true,
                modifier = Modifier.weight(1f)
            )
        }

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("rport (NAT traversal)", fontSize = 14.sp)
            Switch(checked = rport, onCheckedChange = onRportChange)
        }

        HorizontalDivider()

        OutlinedTextField(
            value = username,
            onValueChange = onUsernameChange,
            label = { Text("Username") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )

        OutlinedTextField(
            value = password,
            onValueChange = onPasswordChange,
            label = { Text("Password") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth()
        )

        OutlinedTextField(
            value = displayName,
            onValueChange = onDisplayNameChange,
            label = { Text("Display Name") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )

        HorizontalDivider()

        Text("Transport", fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            listOf("UDP", "TCP", "TLS").forEach { t ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(
                        selected = transport == t,
                        onClick = { onTransportChange(t) }
                    )
                    Text(t, fontSize = 14.sp)
                }
            }
        }
    }
}

@Composable
private fun AudioSettings() {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(
            "Audio settings are managed by the system.",
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            "Use Android system settings to configure audio input/output devices, volumes, and Bluetooth audio routing.",
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        HorizontalDivider()

        Text("Codec: G.711 u-law (PCMU)", fontSize = 14.sp)
        Text("Sample Rate: 8000 Hz", fontSize = 14.sp)
        Text("Frame Size: 20ms (160 samples)", fontSize = 14.sp)
    }
}

@Composable
private fun GeneralSettings(
    theme: String,
    onThemeChange: (String) -> Unit
) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Theme", fontSize = 16.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            listOf("dark" to "Dark", "light" to "Light").forEach { (value, label) ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(
                        selected = theme == value,
                        onClick = { onThemeChange(value) }
                    )
                    Text(label, fontSize = 14.sp)
                }
            }
        }

        Spacer(modifier = Modifier.height(24.dp))
        HorizontalDivider()
        Spacer(modifier = Modifier.height(12.dp))

        // About section
        Text("About", fontSize = 16.sp, color = MaterialTheme.colorScheme.onSurface)
        Spacer(modifier = Modifier.height(4.dp))

        Text(
            "My Line Telecom Softphone",
            fontSize = 15.sp,
            color = MaterialTheme.colorScheme.primary
        )
        Text(
            "Version 2.0.0",
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text(
            "Developed by Roberto Baan",
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurface
        )
        Text(
            "My Line Telecom Corp",
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text(
            "\u00A9 ${java.util.Calendar.getInstance().get(java.util.Calendar.YEAR)} My Line Telecom Corp. All rights reserved.",
            fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}
