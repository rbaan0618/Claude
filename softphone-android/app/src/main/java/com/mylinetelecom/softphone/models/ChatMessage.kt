package com.mylinetelecom.softphone.models

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "chat_messages")
data class ChatMessage(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val remoteNumber: String,
    val body: String,
    val isOutgoing: Boolean,
    val timestamp: Long = System.currentTimeMillis(),
    val status: MessageStatus = MessageStatus.SENT,
    val messageType: String = "sms"   // "sms" or "whatsapp"
)

enum class MessageStatus {
    SENDING,
    SENT,
    FAILED,
    RECEIVED
}
