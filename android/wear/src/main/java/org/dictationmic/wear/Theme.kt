package org.dictationmic.wear

import androidx.compose.ui.graphics.Color
import androidx.wear.compose.material.Colors

// The same palette as the web app and the phone shell, so a note started on the
// wrist and finished on the laptop looks like one product rather than two.
//
// Black first, and not as a style choice: the watch panel is OLED, an unlit
// pixel costs nothing, and this screen spends most of its life dimmed on a
// wrist that is doing something else.
val Ink = Color(0xFF0B0C0A)      // background
val Volt = Color(0xFFB6EE3F)     // the accent, and the only thing that shouts
val Panel = Color(0xFF1A1D18)    // rows and the unfilled part of the level ring
val Muted = Color(0xFF8A8F82)    // status lines and hints
val Chalk = Color(0xFFE8E6E1)    // dictated words
val Warn = Color(0xFFEE675C)     // a refused microphone, a failed sign-in

// Wear's Material components read their defaults from here — the ripple on a
// tap, the clock at the top — so setting it once is what keeps the stock parts
// from arriving in Google's blue.
val WatchColors = Colors(
    primary = Volt,
    primaryVariant = Volt,
    secondary = Volt,
    secondaryVariant = Panel,
    background = Ink,
    surface = Panel,
    error = Warn,
    onPrimary = Ink,
    onSecondary = Ink,
    onBackground = Chalk,
    onSurface = Chalk,
    onSurfaceVariant = Muted,
    onError = Ink,
)
