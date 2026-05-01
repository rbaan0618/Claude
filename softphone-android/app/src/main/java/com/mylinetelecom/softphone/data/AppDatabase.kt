package com.mylinetelecom.softphone.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import com.mylinetelecom.softphone.models.CallRecord
import com.mylinetelecom.softphone.models.ChatMessage
import com.mylinetelecom.softphone.models.Contact

@Database(
    entities = [CallRecord::class, Contact::class, ChatMessage::class],
    version = 3,
    exportSchema = false
)
@androidx.room.TypeConverters(Converters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun callHistoryDao(): CallHistoryDao
    abstract fun contactsDao(): ContactsDao
    abstract fun chatMessageDao(): ChatMessageDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "softphone.db"
                ).fallbackToDestructiveMigration()
                .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
