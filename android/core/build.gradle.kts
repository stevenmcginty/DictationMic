plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

// The dictation engine, note storage and cloud sync, with no interface of any
// kind attached. The phone app draws them as a WebView and the watch app draws
// them as two Compose screens; neither one owns them.
android {
    namespace = "org.dictationmic.android.core"
    compileSdk = 35

    defaultConfig {
        minSdk = 26
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

// `api`, not `implementation`: both apps compile against these types
// (coroutine flows in particular) through this module's own surface.
dependencies {
    api("androidx.core:core-ktx:1.15.0")
    api("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
