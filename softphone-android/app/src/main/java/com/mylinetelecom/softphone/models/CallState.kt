package com.mylinetelecom.softphone.models

enum class CallState {
    IDLE,
    CALLING,
    RINGING,
    INCOMING,
    CONFIRMED,
    HOLD,
    DISCONNECTED,
    REJECTED,
    BUSY
}

enum class CallDirection {
    INBOUND,
    OUTBOUND
}

enum class BlfState {
    IDLE,
    RINGING,
    BUSY,
    UNKNOWN,
    OFFLINE
}

enum class RegistrationState {
    UNREGISTERED,
    REGISTERING,
    REGISTERED,
    FAILED
}
