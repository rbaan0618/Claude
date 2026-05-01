package com.mylinetelecom.softphone.models

data class BlfEntry(
    val extension: String,
    val label: String = "",
    val state: BlfState = BlfState.UNKNOWN
) {
    val displayName: String
        get() = label.ifBlank { extension }
}
