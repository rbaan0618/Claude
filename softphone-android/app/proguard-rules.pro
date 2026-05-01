# Keep SIP/RTP classes
-keep class com.mylinetelecom.softphone.sip.** { *; }
-keep class com.mylinetelecom.softphone.models.** { *; }

# Room
-keep class * extends androidx.room.RoomDatabase
-dontwarn androidx.room.paging.**
