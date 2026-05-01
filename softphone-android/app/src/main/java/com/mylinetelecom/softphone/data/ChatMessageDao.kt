package com.mylinetelecom.softphone.data

import androidx.room.*
import com.mylinetelecom.softphone.models.ChatMessage
import kotlinx.coroutines.flow.Flow

@Dao
interface ChatMessageDao {

    // All messages for a specific conversation (number + channel)
    @Query("SELECT * FROM chat_messages WHERE remoteNumber = :number AND messageType = :type ORDER BY timestamp ASC")
    fun getMessagesForChat(number: String, type: String): Flow<List<ChatMessage>>

    // Last message per (remoteNumber, messageType) pair — used for conversation list
    @Query("""
        SELECT * FROM chat_messages
        WHERE id IN (
            SELECT MAX(id) FROM chat_messages GROUP BY remoteNumber, messageType
        )
        AND messageType = :type
        ORDER BY timestamp DESC
    """)
    fun getConversationsForType(type: String): Flow<List<ChatMessage>>

    // Unread count for a specific conversation
    @Query("SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = :number AND messageType = :type AND isOutgoing = 0 AND status = 'RECEIVED'")
    fun getUnreadCount(number: String, type: String): Flow<Int>

    // Check if a WhatsApp conversation exists for a number (used for inbound type detection)
    @Query("SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = :number AND messageType = :type")
    suspend fun countForType(number: String, type: String): Int

    @Insert
    suspend fun insert(message: ChatMessage): Long

    @Update
    suspend fun update(message: ChatMessage)

    // Duplicate detection: count identical inbound messages within last N milliseconds
    @Query("SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = :number AND body = :body AND isOutgoing = 0 AND timestamp > :since")
    suspend fun countRecentDuplicates(number: String, body: String, since: Long): Int

    @Query("DELETE FROM chat_messages WHERE remoteNumber = :number AND messageType = :type")
    suspend fun deleteConversation(number: String, type: String)

    @Query("DELETE FROM chat_messages")
    suspend fun deleteAll()
}
