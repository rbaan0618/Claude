package com.mylinetelecom.softphone.data

import androidx.room.*
import com.mylinetelecom.softphone.models.CallDirection
import com.mylinetelecom.softphone.models.CallRecord
import kotlinx.coroutines.flow.Flow

@Dao
interface CallHistoryDao {
    @Query("SELECT * FROM call_history ORDER BY startedAt DESC")
    fun getAll(): Flow<List<CallRecord>>

    @Query("SELECT * FROM call_history WHERE direction = :direction ORDER BY startedAt DESC")
    fun getByDirection(direction: CallDirection): Flow<List<CallRecord>>

    @Insert
    suspend fun insert(record: CallRecord): Long

    @Update
    suspend fun update(record: CallRecord)

    @Delete
    suspend fun delete(record: CallRecord)

    @Query("DELETE FROM call_history")
    suspend fun deleteAll()
}
