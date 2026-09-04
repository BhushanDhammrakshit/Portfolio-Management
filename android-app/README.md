# Android WebView App

Thin Android wrapper around the existing Flask web app. No backend or web
frontend code was touched — this is a standalone Gradle project.

## Setup

1. Set your deployed URL in [app/src/main/res/values/strings.xml](app/src/main/res/values/strings.xml)
   (`base_url`). For local testing against `python run.py`, use
   `http://10.0.2.2:5000` (emulator alias for your machine's `localhost`) and
   the app is already allowed cleartext traffic to that host.
2. Open the `android-app` folder in Android Studio (it will generate the
   Gradle wrapper automatically) and let it sync.
3. Generate a launcher icon: right-click `res` → New → Image Asset, then add
   `android:icon="@mipmap/ic_launcher"` to the `<application>` tag in
   [AndroidManifest.xml](app/src/main/AndroidManifest.xml).
4. Run on an emulator or device.

## What's implemented

- WebView with cookies/localStorage enabled (session-based login works as-is).
- File chooser support for the CSV import feature.
- Pull-to-refresh (reloads the current page).
- Back button navigates WebView history before exiting the app.
- External-domain links (e.g. payment gateway) open in the system browser
  instead of the WebView.

## Building a release APK

Use Android Studio's **Build → Generate Signed App Bundle / APK**, or
`gradlew assembleRelease` once you configure a signing config.
