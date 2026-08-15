import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Same keystore as the phone build (android/keystore.properties, gitignored),
// falling back to debug signing so a fresh checkout still builds.
val keystoreProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "org.dictationmic.wear"
    compileSdk = 35

    defaultConfig {
        // A separate application id on purpose: the watch app is a standalone
        // install, and sharing the phone's id would make the two of them
        // upgrades of each other.
        applicationId = "org.dictationmic.wear"
        // Wear OS 4 and up. Below that there is no on-device recogniser worth
        // dictating into.
        minSdk = 33
        targetSdk = 34
        versionCode = 6
        versionName = "1.3.1"
    }

    signingConfigs {
        create("release") {
            if (keystoreProps.isNotEmpty()) {
                storeFile = rootProject.file(keystoreProps["storeFile"] as String)
                storePassword = keystoreProps["storePassword"] as String
                keyAlias = keystoreProps["keyAlias"] as String
                keyPassword = keystoreProps["keyPassword"] as String
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystoreProps.isNotEmpty()) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }

    buildFeatures { compose = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation(project(":core"))
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.compose.ui:ui:1.7.5")
    implementation("androidx.compose.ui:ui-graphics:1.7.5")
    // Wear's own Material: the round-screen layouts (TimeText, ScalingLazyColumn,
    // Chip) that the phone Material components have no idea about.
    implementation("androidx.wear.compose:compose-material:1.4.0")
    implementation("androidx.wear.compose:compose-foundation:1.4.0")
    // The watch keyboard / voice input dialog, for the one-time sign-in.
    implementation("androidx.wear:wear-input:1.1.0")
    // Tiles. Not Compose: a tile is rendered by the system launcher in its own
    // process, so its layout is a protolayout description shipped across, not
    // a composable this app draws.
    implementation("androidx.wear.tiles:tiles:1.4.1")
    implementation("androidx.wear.protolayout:protolayout:1.2.1")
    // TileService answers in ListenableFutures. This is the small resolvable
    // implementation of one — the alternative is pulling all of Guava onto a
    // watch to call a single immediateFuture.
    implementation("androidx.concurrent:concurrent-futures:1.2.0")
    // Complications: the mic slot on the watch face itself.
    implementation("androidx.wear.watchface:watchface-complications-data-source-ktx:1.2.1")
    // The watch face: Steve's daily face is ours now — time, steps, heart
    // rate, the mic dead centre, and the battery around the rim.
    implementation("androidx.wear.watchface:watchface:1.2.1")
    implementation("androidx.wear.watchface:watchface-complications-rendering:1.2.1")
}
