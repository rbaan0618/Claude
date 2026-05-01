package com.mylinetelecom.softphone.models

data class SipConfig(
    val server: String = "",
    val port: Int = 5060,
    val localPort: Int = 5060,
    val rport: Boolean = true,
    val username: String = "",
    val password: String = "",
    val displayName: String = "",
    val transport: String = "UDP",
    val enabled: Boolean = true
) {
    val isValid: Boolean
        get() = server.isNotBlank() && username.isNotBlank() && password.isNotBlank()

    val domain: String
        get() = if (server.contains(".")) server else "$server.myline.tel"
}
