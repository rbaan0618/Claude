package com.mylinetelecom.softphone.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import com.mylinetelecom.softphone.models.BlfEntry
import com.mylinetelecom.softphone.models.SipConfig
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import org.json.JSONArray
import org.json.JSONObject

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

class SettingsRepository(private val context: Context) {
    companion object {
        // SIP keys
        val SIP_SERVER = stringPreferencesKey("sip_server")
        val SIP_PORT = intPreferencesKey("sip_port")
        val SIP_LOCAL_PORT = intPreferencesKey("sip_local_port")
        val SIP_RPORT = booleanPreferencesKey("sip_rport")
        val SIP_USERNAME = stringPreferencesKey("sip_username")
        val SIP_PASSWORD = stringPreferencesKey("sip_password")
        val SIP_DISPLAY_NAME = stringPreferencesKey("sip_display_name")
        val SIP_TRANSPORT = stringPreferencesKey("sip_transport")
        val SIP_ENABLED = booleanPreferencesKey("sip_enabled")

        // GUI keys
        val THEME = stringPreferencesKey("theme")

        // BLF keys
        val BLF_ENTRIES = stringPreferencesKey("blf_entries")
    }

    val sipConfig: Flow<SipConfig> = context.dataStore.data.map { prefs ->
        SipConfig(
            server = prefs[SIP_SERVER] ?: "",
            port = prefs[SIP_PORT] ?: 5060,
            localPort = prefs[SIP_LOCAL_PORT] ?: 5060,
            rport = prefs[SIP_RPORT] ?: true,
            username = prefs[SIP_USERNAME] ?: "",
            password = prefs[SIP_PASSWORD] ?: "",
            displayName = prefs[SIP_DISPLAY_NAME] ?: "",
            transport = prefs[SIP_TRANSPORT] ?: "UDP",
            enabled = prefs[SIP_ENABLED] ?: true
        )
    }

    val theme: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[THEME] ?: "dark"
    }

    val blfEntries: Flow<List<BlfEntry>> = context.dataStore.data.map { prefs ->
        val json = prefs[BLF_ENTRIES] ?: "[]"
        try {
            val array = JSONArray(json)
            (0 until array.length()).map { i ->
                val obj = array.getJSONObject(i)
                BlfEntry(
                    extension = obj.getString("extension"),
                    label = obj.optString("label", "")
                )
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    suspend fun saveSipConfig(config: SipConfig) {
        context.dataStore.edit { prefs ->
            prefs[SIP_SERVER] = config.server
            prefs[SIP_PORT] = config.port
            prefs[SIP_LOCAL_PORT] = config.localPort
            prefs[SIP_RPORT] = config.rport
            prefs[SIP_USERNAME] = config.username
            prefs[SIP_PASSWORD] = config.password
            prefs[SIP_DISPLAY_NAME] = config.displayName
            prefs[SIP_TRANSPORT] = config.transport
            prefs[SIP_ENABLED] = config.enabled
        }
    }

    suspend fun saveTheme(theme: String) {
        context.dataStore.edit { prefs ->
            prefs[THEME] = theme
        }
    }

    suspend fun saveBlfEntries(entries: List<BlfEntry>) {
        val json = JSONArray().apply {
            entries.forEach { entry ->
                put(JSONObject().apply {
                    put("extension", entry.extension)
                    put("label", entry.label)
                })
            }
        }
        context.dataStore.edit { prefs ->
            prefs[BLF_ENTRIES] = json.toString()
        }
    }
}
