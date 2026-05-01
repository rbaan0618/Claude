# My Line Telecom Softphone — iOS

Native iOS SIP softphone, Swift + SwiftUI. Functional port of
`../softphone-android`, preserving the same SIP dialog state machine, codec set,
and feature list (dialpad, history, contacts, messages, BLF, attended transfer).

**Status:** scaffold / work in progress. Fully-ported modules: models, StunClient,
SIP message builder/parser, data layer, SwiftUI screens, CallKit + PushKit
integration. Stubbed with TODOs: the inner dispatch of SipHandler (REGISTER,
INVITE, re-INVITE hold, REFER, SUBSCRIBE/NOTIFY, MESSAGE) and the RtpSession
codec pipeline (G.711 tables + GSM + G.729 bridging).

## Project layout

```
softphone-ios/
├── project.yml                 XcodeGen spec (run `xcodegen generate` on a Mac)
├── README.md
└── MyLineSoftphone/
    ├── Info.plist
    ├── MyLineSoftphone.entitlements
    ├── BCG729-Bridging-Header.h
    ├── MyLineSoftphoneApp.swift        @main entry
    ├── ContentView.swift               root tab navigation
    ├── Models/
    │   ├── SipConfig.swift
    │   ├── CallState.swift             CallState / CallDirection / BlfState / RegistrationState
    │   ├── CallRecord.swift
    │   ├── Contact.swift
    │   ├── ChatMessage.swift
    │   └── BlfEntry.swift
    ├── SIP/
    │   ├── StunClient.swift            full 1:1 port
    │   ├── SipMessageBuilder.swift     request/response/digest/SDP builders
    │   ├── SipMessageParser.swift      header lookups + auth challenge + SDP parser
    │   ├── SipHandler.swift            dialog state machine (skeleton + TODOs)
    │   └── RtpSession.swift            RTP audio pipeline (skeleton)
    ├── Services/
    │   └── SipService.swift            CallKit + PushKit bridge, top-level service
    ├── Data/
    │   ├── AppDatabase.swift           GRDB schema + migrations
    │   ├── CallHistoryDao.swift
    │   ├── ContactsDao.swift
    │   ├── ChatMessageDao.swift
    │   └── SettingsRepository.swift    UserDefaults + Keychain
    └── UI/
        ├── Components/
        │   └── DialpadButton.swift
        └── Screens/
            ├── DialpadScreen.swift
            ├── InCallScreen.swift
            ├── CallHistoryScreen.swift
            ├── ContactsScreen.swift
            ├── MessagesScreen.swift
            ├── ChatDetailScreen.swift
            ├── BlfScreen.swift
            ├── SettingsScreen.swift
            └── TransferDialog.swift
```

## Building on macOS

You need a Mac with Xcode 15+ to compile and run. This repo has **no committed
`.xcodeproj`** — we use [XcodeGen](https://github.com/yonaskolb/XcodeGen) to
generate it from `project.yml`, so the tree stays diffable.

```bash
# One-time tool install
brew install xcodegen

# From the softphone-ios/ directory:
xcodegen generate

# Open the generated project
open MyLineSoftphone.xcodeproj

# Xcode will fetch GRDB via Swift Package Manager on first build.
```

Then in Xcode:

1. Select a development team in **Signing & Capabilities** for the
   `MyLineSoftphone` target.
2. Enable capabilities:
   - Background Modes → Voice over IP, Audio, Push Notifications
   - Push Notifications (only if you wire up the VoIP push bridge)
3. Set the SIP server + credentials in the **Settings** tab on first launch.
4. Run on a **physical device**. The simulator cannot record from a real
   microphone and CallKit has limited simulator support.

## Architecture parity with Android

| Android (Kotlin)                    | iOS (Swift)                        | Notes |
|-------------------------------------|------------------------------------|-------|
| `SipHandler.kt` (2297 lines)        | `SIP/SipHandler.swift`             | Same state machine, serial queue instead of Coroutine `Dispatchers.IO` |
| `RtpSession.kt`                     | `SIP/RtpSession.swift`             | `AVAudioEngine` at 8 kHz mono; hardware AEC via `.voiceChat` mode |
| `StunClient.kt`                     | `SIP/StunClient.swift`             | Full 1:1 port, shares socket fd with SIP |
| `GsmCodec.kt` + `cpp/bcg729/`       | `GsmCodec` class + bridging header | C sources copy as-is via `BCG729-Bridging-Header.h` |
| `service/SipService.kt` (foreground)| `Services/SipService.swift`        | CallKit + PushKit; no Android-style foreground service on iOS |
| `data/AppDatabase.kt` (Room)        | `Data/AppDatabase.swift` (GRDB)    | Same three tables with compatible schema |
| `SettingsRepository.kt` (DataStore) | `Data/SettingsRepository.swift`    | UserDefaults + Keychain (password is now in secure storage) |
| Jetpack Compose screens             | SwiftUI screens                    | One-to-one view mapping |
| `AndroidManifest.xml` permissions   | `Info.plist` + entitlements        | mic, network, VoIP, audio bg modes |

## Background behavior — important iOS caveat

Unlike Android's foreground service, iOS cannot keep a plain UDP socket alive
indefinitely while the app is suspended. There are **two support paths**:

1. **Without VoIP push** (easier, v1 default): registration is maintained while
   the app is foregrounded or an audio call is active. The app loses
   registration ~30 s after backgrounding. Outbound calls and in-foreground
   incoming calls work.

2. **With VoIP push** (production): run a small server next to the SIP PBX that
   subscribes to INVITE events (Asterisk AMI, FreeSWITCH ESL, Kamailio dispatcher
   module, …) and sends an APNs VoIP push to the device token registered in
   `SipService.pushRegistry(didUpdate:)`. The incoming push wakes the app, and
   inside `didReceiveIncomingPushWith` it **must** call `CXProvider.reportNewIncomingCall`
   before returning — this is an Apple policy enforced at runtime.

The scaffold already contains the PushKit entry points; only the server-side
bridge and the token upload are left as TODO.

## What's left to port

In rough priority order:

1. **SipHandler message dispatch bodies.** All the `// TODO:` markers in
   `SipHandler.swift` — each points at a specific line range in the Kotlin
   source. Start with REGISTER → INVITE → BYE → re-INVITE (hold).
2. **RtpSession audio pipeline.** Wire `AVAudioEngine` input tap → PCMU encode →
   RTP packet → `sendto`. Then the receive path in reverse.
3. **G.711 tables** in `RtpSession.swift` — copy `ULAW_EXPONENT_TABLE` and
   `ULAW_DECODE_TABLE` from Kotlin.
4. **Copy bcg729 sources** from `softphone-android/app/src/main/cpp/bcg729/` into
   `MyLineSoftphone/Codecs/bcg729/` and `#include "bcg729/encoder.h"` in the
   bridging header. Add the `.c` files to the Xcode target.
5. **G.729 + GSM Swift wrappers** around the C APIs.
6. **CallKit polish**: hold/unhold, DTMF, audio route changes, call update with
   remote party name once the SIP dialog confirms.
7. **Push bridge** for background incoming calls.

## License

Same as the Android project.
