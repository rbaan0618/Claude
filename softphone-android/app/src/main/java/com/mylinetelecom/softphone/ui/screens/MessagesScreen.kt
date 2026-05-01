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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mylinetelecom.softphone.models.ChatMessage
import com.mylinetelecom.softphone.models.Contact
import com.mylinetelecom.softphone.ui.theme.BrandPrimary
import java.text.SimpleDateFormat
import java.util.*

val WhatsAppGreen = Color(0xFF25D366)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MessagesScreen(
    smsConversations: List<ChatMessage>,
    waConversations: List<ChatMessage>,
    contacts: List<Contact>,
    onOpenChat: (number: String, type: String) -> Unit,
    onNewMessage: () -> Unit,
    onDeleteConversation: (number: String, type: String) -> Unit
) {
    val contactMap = remember(contacts) { contacts.associateBy { it.number } }
    var selectedTab by remember { mutableIntStateOf(0) }
    val tabs = listOf("SMS", "WhatsApp")

    Column(modifier = Modifier.fillMaxSize()) {
        // Header
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 8.dp, top = 12.dp, bottom = 4.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Messages", fontSize = 24.sp, color = MaterialTheme.colorScheme.onSurface)
            IconButton(onClick = onNewMessage) {
                Icon(Icons.Default.Edit, "New message", tint = BrandPrimary)
            }
        }

        // Tabs
        TabRow(
            selectedTabIndex = selectedTab,
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = if (selectedTab == 0) BrandPrimary else WhatsAppGreen
        ) {
            tabs.forEachIndexed { index, title ->
                Tab(
                    selected = selectedTab == index,
                    onClick = { selectedTab = index },
                    text = { Text(title) },
                    icon = {
                        if (index == 0) {
                            Icon(Icons.Default.Sms, contentDescription = null)
                        } else {
                            Icon(Icons.Default.Chat, contentDescription = null,
                                tint = if (selectedTab == 1) WhatsAppGreen
                                       else MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    },
                    selectedContentColor = if (index == 0) BrandPrimary else WhatsAppGreen,
                    unselectedContentColor = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        // Conversation list
        val activeConversations = if (selectedTab == 0) smsConversations else waConversations
        val activeType = if (selectedTab == 0) "sms" else "whatsapp"
        val accentColor = if (selectedTab == 0) BrandPrimary else WhatsAppGreen

        if (activeConversations.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(
                        if (selectedTab == 0) Icons.Default.Sms else Icons.Default.Chat,
                        contentDescription = null,
                        modifier = Modifier.size(48.dp),
                        tint = accentColor.copy(alpha = 0.4f)
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        if (selectedTab == 0) "No SMS conversations yet"
                        else "No WhatsApp conversations yet",
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp)
            ) {
                items(activeConversations, key = { "${it.remoteNumber}|${it.messageType}" }) { lastMessage ->
                    ConversationItem(
                        lastMessage = lastMessage,
                        contactName = contactMap[lastMessage.remoteNumber]?.name,
                        accentColor = accentColor,
                        onClick = { onOpenChat(lastMessage.remoteNumber, activeType) },
                        onDelete = { onDeleteConversation(lastMessage.remoteNumber, activeType) }
                    )
                }
            }
        }
    }
}

@Composable
private fun ConversationItem(
    lastMessage: ChatMessage,
    contactName: String?,
    accentColor: Color,
    onClick: () -> Unit,
    onDelete: () -> Unit
) {
    var showMenu by remember { mutableStateOf(false) }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Avatar
            Surface(
                modifier = Modifier.size(42.dp),
                shape = MaterialTheme.shapes.extraLarge,
                color = accentColor.copy(alpha = 0.15f)
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Text(
                        (contactName ?: lastMessage.remoteNumber).take(1).uppercase(),
                        fontSize = 18.sp,
                        color = accentColor
                    )
                }
            }

            Spacer(modifier = Modifier.width(12.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    contactName ?: lastMessage.remoteNumber,
                    fontSize = 16.sp,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Text(
                    (if (lastMessage.isOutgoing) "You: " else "") + lastMessage.body,
                    fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }

            Spacer(modifier = Modifier.width(8.dp))

            Text(
                formatMessageTime(lastMessage.timestamp),
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )

            // More menu
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

private fun formatMessageTime(timestamp: Long): String {
    val now = System.currentTimeMillis()
    val diff = now - timestamp
    val sdf = if (diff < 24 * 60 * 60 * 1000) {
        SimpleDateFormat("HH:mm", Locale.getDefault())
    } else {
        SimpleDateFormat("MMM d", Locale.getDefault())
    }
    return sdf.format(Date(timestamp))
}
