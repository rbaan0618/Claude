package com.mylinetelecom.softphone.models

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "call_history")
data class CallRecord(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val direction: CallDirection,
    val remoteNumber: String,
    val remoteName: String = "",
    val status: String = "",
    val startedAt: Long = System.currentTimeMillis(),
    val answeredAt: Long? = null,
    val endedAt: Long? = null,
    val durationSeconds: Int = 0
)
