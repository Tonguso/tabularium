# Agents Android

Small Android client for the existing private dashboard. Android 8 or newer. The app uses the system WebView,
the Android document picker, and microphone permission requested only when Record voice is pressed. No analytics,
advertising, cloud messaging service, broad storage access, JavaScript/native bridge, or cleartext network access.

The app accepts an HTTPS Tailscale dashboard address and pins its WebView navigation and microphone permission
to that exact origin. Tailscale must be connected. The server still checks Tailscale identity and CSRF.
Selecting a file or stopping a recording saves a local draft; only Send submits it. Keep the app open for uploads.
One attachment per message, 50 MB maximum (audio 25 MB), voice recordings up to five minutes. Replies are text in this version.
Push notifications, background uploads and Android share-target integration are not implemented.

## Build

Use JDK 17 and Android SDK platform 35 / build-tools 35.0.0. No Gradle or third-party app library is required.
Create and retain a private signing keystore outside this source tree. Reuse it for every update, and increment
the manifest versionCode for releases. Losing the key prevents in-place updates. Never commit it or its password.

Run `python build.py --java <jdk-directory> --sdk <sdk-directory> --keystore <key-file> --password-file <password-file>`.
An optional `--url https://your-pc.your-tailnet.ts.net` embeds the initial address in the ignored build output;
otherwise the app asks for it. Sources contain no owner's hostname. The build runs executable origin-policy tests
and verifies the APK signature. Output: `build/agents.apk` and its SHA-256 file.

The dashboard's authenticated `/agents.apk` route serves that build. Android may require permission to install
from your browser. Installation, microphone permission and file-picker behavior still require an Android device
check; a successful build and browser tests alone do not prove those device-specific flows.
