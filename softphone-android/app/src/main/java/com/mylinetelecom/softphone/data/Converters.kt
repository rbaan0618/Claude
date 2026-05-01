package com.mylinetelecom.softphone.data

import androidx.room.TypeConverter
import com.mylinetelecom.softphone.models.CallDirection
import com.mylinetelecom.softphone.models.MessageStatus

class Converters {
    @TypeConverter
    fun fromCallDirection(direction: CallDirection): String = direction.name

    @TypeConverter
    fun toCallDirection(value: String): CallDirection = CallDirection.valueOf(value)

    @TypeConverter
    fun fromMessageStatus(status: MessageStatus): String = status.name

    @TypeConverter
    fun toMessageStatus(value: String): MessageStatus = MessageStatus.valueOf(value)
}
