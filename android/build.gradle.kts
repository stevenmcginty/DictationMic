plugins {
    id("com.android.application") version "8.7.3" apply false
    id("com.android.library") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // The watch interface is Compose; Kotlin 2.0 ships the compiler as a plugin.
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}
