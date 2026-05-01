package com.mylinetelecom.softphone.ui.theme

import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Dark theme colors (Catppuccin-inspired, matching Python version)
val DarkBackground = Color(0xFF1E1E2E)
val DarkSurface = Color(0xFF313244)
val DarkSurfaceVariant = Color(0xFF45475A)
val DarkText = Color(0xFFCDD6F4)
val DarkSubtext = Color(0xFFA6ADC8)
val BrandPrimary = Color(0xFF1E88E5)
val BrandDark = Color(0xFF1565C0)

val CallGreen = Color(0xFF4CAF50)
val CallRed = Color(0xFFF44336)
val CallOrange = Color(0xFFFF9800)
val HoldColor = Color(0xFFFF5722)
val MuteColor = Color(0xFFF44336)

val BlfIdle = Color(0xFF4CAF50)
val BlfRinging = Color(0xFFFF9800)
val BlfBusy = Color(0xFFF44336)
val BlfUnknown = Color(0xFF9E9E9E)
val BlfOffline = Color(0xFF616161)

// Light theme colors
val LightBackground = Color(0xFFF5F5F5)
val LightSurface = Color(0xFFFFFFFF)
val LightSurfaceVariant = Color(0xFFE0E0E0)
val LightText = Color(0xFF212121)
val LightSubtext = Color(0xFF757575)

private val DarkColorScheme = darkColorScheme(
    primary = BrandPrimary,
    onPrimary = Color.White,
    secondary = BrandDark,
    background = DarkBackground,
    surface = DarkSurface,
    surfaceVariant = DarkSurfaceVariant,
    onBackground = DarkText,
    onSurface = DarkText,
    onSurfaceVariant = DarkSubtext,
    error = CallRed
)

private val LightColorScheme = lightColorScheme(
    primary = BrandPrimary,
    onPrimary = Color.White,
    secondary = BrandDark,
    background = LightBackground,
    surface = LightSurface,
    surfaceVariant = LightSurfaceVariant,
    onBackground = LightText,
    onSurface = LightText,
    onSurfaceVariant = LightSubtext,
    error = CallRed
)

@Composable
fun MyLineSoftphoneTheme(
    darkTheme: Boolean = true,
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme

    MaterialTheme(
        colorScheme = colorScheme,
        content = content
    )
}
