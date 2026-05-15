# iOS Softphone Deployment Guide — My Line Telecom

**Version:** 1.0
**Updated:** 2026-05-15
**Scope:** Step-by-step setup of the iOS softphone (`MyLineTelecom-iOS`) — Apple Developer configuration, Xcode project setup, dSIPRouter SBC push integration, and CI/CD via Codemagic.
**Audience:** Engineer building the iOS app for the first time, onboarding a new TestFlight team, or replicating the SBC push setup on a new dSIPRouter.

For the WhatsApp side see [`whatsapp-deployment-guide.md`](whatsapp-deployment-guide.md).
For the FusionPBX + general SBC + SMS setup see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md).

---

## Table of contents

- **Part 1** — [Apple Developer account setup](#part-1--apple-developer-account-setup)
- **Part 2** — [Xcode project configuration](#part-2--xcode-project-configuration)
- **Part 3** — [How the iOS app talks to the SBC](#part-3--how-the-ios-app-talks-to-the-sbc)
- **Part 4** — [Codemagic CI/CD](#part-4--codemagic-cicd)
- **Part 5** — [dSIPRouter push server (Python)](#part-5--dsiprouter-push-server-python)
- **Part 6** — [dSIPRouter kamailio.cfg push routes](#part-6--dsiprouter-kamailiocfg-push-routes)
- **Part 7** — [TestFlight & App Store production](#part-7--testflight--app-store-production)
- **Part 8** — [Verification](#part-8--verification)
- **Part 9** — [Troubleshooting](#part-9--troubleshooting)
- **Part 10** — [File inventory](#part-10--file-inventory)

---

## Part 1 — Apple Developer account setup

Do this **once** for the company, then add team members. Required: paid Apple Developer Program membership ($99/year).

### Step 1.1 — Apple Developer account

1. Go to https://developer.apple.com/account
2. **Membership → Enroll** if not already a member
3. Choose **Organization** (you'll need DUNS number) or **Individual**
4. Pay $99 — approval takes 1–7 business days
5. Note your **Team ID** (top-right of Account page, 10 chars, e.g. `EXAMPLEXYZ`) — you'll need it for the push server

### Step 1.2 — Create the App ID

The App ID identifies your app to Apple's services (push, in-app purchase, etc.). Each new app needs one.

1. **Certificates, Identifiers & Profiles → Identifiers → +**
2. Type: **App IDs → App**
3. Description: `My Line Telecom Softphone`
4. Bundle ID (Explicit): `com.yourcompany.softphone`
   - This **must match** the bundle ID in your Xcode project (Part 2.1)
   - Reverse-DNS format. Once registered with App Store, it's hard to change.
5. **Capabilities** — check ALL of these:
   - ☑ **Push Notifications** — required for VoIP push wakeup
   - ☑ **Background Modes** (no separate option in this UI — set in Xcode)
6. **Continue → Register**

### Step 1.3 — Generate the APNs Auth Key (.p8)

This key signs the JWT we send to Apple's APNs service to authenticate push notifications. **Use one Auth Key per Apple Developer account** — it works for ALL apps + dev/prod environments.

1. **Certificates, Identifiers & Profiles → Keys → +**
2. Key Name: `MyLine VoIP Push Key`
3. Capabilities: ☑ **Apple Push Notifications service (APNs)**
4. **Continue → Register**
5. **Download the .p8 file** — Apple lets you download it ONCE only. Save it securely. If you lose it, generate a new one.
6. Note the **Key ID** (10 chars, shown after creation, e.g. `ABCD1234XY`). You'll need this in the push server (Part 5.2).

The `.p8` file contains an EC P-256 private key in PEM format. It looks like:
```
-----BEGIN PRIVATE KEY-----
MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg...
-----END PRIVATE KEY-----
```

### Step 1.4 — Provisioning profiles

For local development and TestFlight builds, Xcode can manage these automatically (**Automatically manage signing**). For Codemagic CI you'll need the certs + provisioning profiles exported manually:

1. **Certificates → +** — generate **Apple Development** (for debug) and **Apple Distribution** (for TestFlight/App Store) certs. Each requires a CSR file from Keychain Access on a Mac.
2. **Profiles → +** — generate matching profiles bound to your App ID:
   - **iOS App Development** — for local debug builds on test devices
   - **App Store** — for TestFlight + App Store submission
3. Add your iPhone(s) to **Devices** (need UDID — find in Xcode Devices window or Apple Configurator)

For Codemagic, see Part 4.2 for upload format.

---

## Part 2 — Xcode project configuration

The iOS app source is under `softphone-ios/MyLineSoftphone/` in this repo. These are the project settings that must match Part 1.

### Step 2.1 — Bundle Identifier

In Xcode: **MyLineSoftphone target → General → Identity → Bundle Identifier**

Set to the **same value** you used in Part 1.2:
```
com.yourcompany.softphone
```

If you change this later, you must update:
- Apple Developer App ID
- iOS app entitlements
- `voip_push_server.py` `APNS_BUNDLE_ID` constant
- Re-generate provisioning profiles

### Step 2.2 — Capabilities

In Xcode: **MyLineSoftphone target → Signing & Capabilities → +Capability**

Add ALL of these:

| Capability | Why |
|------------|-----|
| **Push Notifications** | Required for PushKit / VoIP push to work |
| **Background Modes** | Sub-checkboxes below |

In **Background Modes**, check:

| Mode | Why |
|------|-----|
| ☑ **Voice over IP** | Allows PushKit registration; tells iOS this is a VoIP app |
| ☑ **Audio, AirPlay, and Picture in Picture** | Allows audio to continue playing in background during a call |
| ☑ **Remote notifications** | Allows the app to receive standard APNs (not strictly required if only PushKit, but good safety net) |

### Step 2.3 — Entitlements file

`MyLineSoftphone/MyLineSoftphone.entitlements` controls which Apple services your app uses:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>aps-environment</key>
    <string>development</string>      <!-- ⚠️ change to "production" for App Store builds -->
</dict>
</plist>
```

**Critical:** `aps-environment` value MUST match your APNs endpoint in `voip_push_server.py`:

| Build type | `aps-environment` | `APNS_HOST` in push server |
|------------|-------------------|---------------------------|
| Local Xcode debug | `development` | `https://api.sandbox.push.apple.com` |
| TestFlight / App Store | `production` | `https://api.push.apple.com` |

Mismatch causes APNs error `400 BadDeviceToken` — and pushes silently fail. This is the single most common deployment mistake.

For Codemagic builds intended for TestFlight, you'll need a separate entitlements file with `production` value (or use build configurations).

### Step 2.4 — Info.plist

`MyLineSoftphone/Info.plist` has the runtime declarations. Key entries:

```xml
<key>UIBackgroundModes</key>
<array>
    <string>audio</string>                <!-- audio playback during call -->
    <string>voip</string>                 <!-- PushKit / VoIP support -->
    <string>remote-notification</string>  <!-- standard APNs fallback -->
</array>

<key>NSMicrophoneUsageDescription</key>
<string>Microphone access is required to make and receive SIP voice calls.</string>

<key>NSLocalNetworkUsageDescription</key>
<string>Local network access is required to communicate with your SIP server.</string>

<key>NSContactsUsageDescription</key>
<string>Contacts access lets you dial people from your address book.</string>

<key>ITSAppUsesNonExemptEncryption</key>
<false/>     <!-- avoids the ITAR encryption questions on App Store submit -->
```

If iOS crashes on first call attempt with "Microphone access denied", the `NSMicrophoneUsageDescription` string is missing or wrong.

### Step 2.5 — Build settings

| Setting | Value | Why |
|---------|-------|-----|
| **iOS Deployment Target** | 15.0 or higher | PushKit + CallKit features used require iOS 14+; we use 15+ for SwiftUI lifecycle |
| **Swift Language Version** | Swift 5 | |
| **Build Active Architecture Only** | Debug=Yes, Release=No | Standard |
| **Code Signing Style** | Automatic (local) / Manual (Codemagic) | |

---

## Part 3 — How the iOS app talks to the SBC

Knowing the SIP-level contract helps debug issues without rebuilding the app.

### Step 3.1 — Registration sends the push token in Contact

When PushKit gives the app a device token, the app appends it to its SIP `Contact` header as a URI parameter:

```
Contact: <sip:231@76.203.175.9:5090;transport=udp
                ;pn-provider=apns
                ;pn-param=com.yourcompany.softphone.voip
                ;pn-prid=478444f248c9ad0ba98dc427b23cc7779e15a0056ab6dde280657e099de7783a>
```

The SBC's REGISTER handler:
1. Saves the registration in `location` table (per-tenant)
2. Extracts `pn-prid=...` via regex
3. Stores `USER@DOMAIN → token` in the `pushtok` htable + `dsip_push_tokens` DB table
4. Rewrites the `Contact` header to `sip:USER@sbc.myline.tel:5060` BEFORE forwarding to FusionPBX (so FusionPBX always routes inbound calls back through the SBC)

Source code: `softphone-ios/MyLineSoftphone/SIP/SipHandler.swift` (REGISTER builder)
SBC handler: see Part 6.1

### Step 3.2 — Inbound call wakes the suspended app

```
1. Caller dials the iPhone's number
2. FusionPBX rings the registered Contact (= sbc.myline.tel:5060)
3. SBC receives INVITE → tries lookup() on local registration
4. Either:
   a. iPhone is awake (recent OPTIONS keepalive / NAT alive)  → INVITE relayed directly
   b. iPhone is suspended (NAT closed)                         → SBC route[SUSPEND] suspends INVITE
5. (case b only) SBC route[SENDPUSH] POSTs to voip_push_server.py
6. (case b only) Push server signs APNs JWT, calls Apple HTTP/2
7. Apple delivers VoIP push to iPhone (~200 ms)
8. iOS wakes the app's PushKit handler — must call CXProvider.reportNewIncomingCall SYNCHRONOUSLY before return
9. App calls sipHandler.start() → fresh SIP REGISTER → JOIN htable → t_continue() resumes the suspended INVITE
10. INVITE relayed to iPhone, CallKit displays incoming call
```

If any step fails, audio doesn't connect. See Part 9 for diagnostics.

### Step 3.3 — Outbound calls go through the SBC

```
1. iPhone CallKit answer or user dials → app sends SIP INVITE
2. SBC receives INVITE
3. SBC routing rules forward to:
   • FusionPBX (if dialing an internal extension or via that PBX's trunk)
   • Meta SIP gateway (if calling a WhatsApp number — see whatsapp-deployment-guide.md Part 4)
4. Audio anchored at SBC's RTPEngine (handles iPhone NAT translation)
```

### Step 3.4 — Critical iOS-side behaviors

The app handles a few things that the SBC depends on. If the app has a bug here, the SBC alone can't compensate:

| App behavior | Why it matters | Source |
|--------------|----------------|--------|
| Sends OPTIONS keepalive every ~15 sec | Keeps NAT pinhole alive so inbound calls reach the device without push | `SipHandler.swift` keepalive timer |
| Calls `CXProvider.reportNewIncomingCall` synchronously in PushKit handler | Apple kills apps that don't report a call within seconds of receiving a VoIP push | `SipService.swift` `pushRegistry(_:didReceiveIncomingPushWith:...)` |
| Echoes ALL Via headers in BYE 200 OK | Otherwise FusionPBX retransmits BYE forever | known iOS library quirk — see SBC fix in Part 6.8 |
| Sends ACK after 200 OK INVITE | Otherwise far side considers call not established → drops it | standard SIP — but if this regresses, audio fails immediately on outbound |
| Stops sending RTP when call ends | Otherwise battery drain + remote sees ghost media | `RtpSession.swift` cleanup |

---

## Part 4 — Codemagic CI/CD

The repo's `codemagic.yaml` builds the iOS app on every push to `master`. Builds take ~10–15 min and produce an `.ipa` for TestFlight.

### Step 4.1 — `codemagic.yaml`

Already in the repo at the root. Key sections:

```yaml
workflows:
  ios-workflow:
    name: iOS Build
    instance_type: mac_mini_m2
    max_build_duration: 60
    environment:
      vars:
        BUNDLE_ID: "com.yourcompany.softphone"
        APP_STORE_APPLE_ID: "YOUR_APP_STORE_CONNECT_APP_ID"
      xcode: latest
      cocoapods: default
    scripts:
      - name: Build .ipa
        script: |
          xcode-project use-profiles
          xcode-project build-ipa --workspace MyLineSoftphone.xcworkspace --scheme MyLineSoftphone
    artifacts:
      - build/ios/ipa/*.ipa
    publishing:
      app_store_connect:
        api_key: $APP_STORE_CONNECT_PRIVATE_KEY
        key_id: $APP_STORE_CONNECT_KEY_IDENTIFIER
        issuer_id: $APP_STORE_CONNECT_ISSUER_ID
        submit_to_testflight: true
```

### Step 4.2 — Signing certs in Codemagic

Codemagic needs your Apple Developer **certificate** and **provisioning profile** to sign the build:

1. **Codemagic UI → App Settings → Code signing identities → iOS**
2. Upload your **Apple Distribution certificate** (`.p12` exported from Keychain on a Mac, with the password)
3. Upload your **App Store provisioning profile** (`.mobileprovision`)
4. Codemagic auto-injects these during build via `xcode-project use-profiles`

For App Store Connect publishing (TestFlight upload), generate an **App Store Connect API Key**:

1. App Store Connect → **Users and Access → Keys → +**
2. Role: **Admin** (or **App Manager**)
3. Download the `.p8` (different from APNs key — this one is for App Store Connect API)
4. Upload to Codemagic → **Environment variables** as `APP_STORE_CONNECT_PRIVATE_KEY` (secure)
5. Set `APP_STORE_CONNECT_KEY_IDENTIFIER` and `APP_STORE_CONNECT_ISSUER_ID` from the same screen

### Step 4.3 — Build trigger

Codemagic webhook fires on every `git push origin master`. To build manually: **Codemagic UI → Start new build**.

A successful build:
1. Compiles + signs the `.ipa`
2. Uploads to App Store Connect (TestFlight)
3. After Apple processes (5–30 min), TestFlight makes the build available to internal testers

To install on your iPhone: open **TestFlight app → My Line Telecom → Install/Update**.

---

## Part 5 — dSIPRouter push server (Python)

This is the HTTP service that signs the APNs JWT and POSTs to Apple. It lives on the dSIPRouter SBC.

Source files in this repo:
- [`dsiprouter-deploy/voip_push_server.py`](../dsiprouter-deploy/voip_push_server.py) — the server (Python 3, ~90 lines)
- [`dsiprouter-deploy/voip-push.service`](../dsiprouter-deploy/voip-push.service) — systemd unit

### Step 5.1 — Install Python dependencies

```bash
apt update
apt install -y python3 python3-cryptography curl
python3 -c "from cryptography.hazmat.primitives.asymmetric import ec; print('OK')"   # smoke test
```

### Step 5.2 — Place the APNs key

```bash
mkdir -p /etc/myline
# Copy the .p8 you downloaded in Part 1.3:
scp AuthKey_ABCD1234XY.p8 root@sbc.myline.tel:/etc/myline/
chmod 600 /etc/myline/AuthKey_ABCD1234XY.p8
chown root:root /etc/myline/AuthKey_ABCD1234XY.p8
```

### Step 5.3 — Deploy the push server

```bash
mkdir -p /opt/myline
scp dsiprouter-deploy/voip_push_server.py root@sbc.myline.tel:/opt/myline/
chmod 755 /opt/myline/voip_push_server.py
```

Then edit `/opt/myline/voip_push_server.py` and fill in the constants at the top:

```python
APNS_KEY_FILE  = '/etc/myline/AuthKey_ABCD1234XY.p8'    # match the file name from Step 5.2
APNS_KEY_ID    = 'ABCD1234XY'                            # from Apple Dev portal (Part 1.3)
APNS_TEAM_ID   = 'EXAMPLEXYZ'                            # from Apple Dev account (Part 1.1)
APNS_BUNDLE_ID = 'com.yourcompany.softphone'             # match Xcode bundle (Part 2.1)
PUSH_SECRET    = 'a-strong-random-string-32-chars-min'   # MUST match kamailio (Part 6.3)
APNS_HOST      = 'https://api.push.apple.com'            # production OR sandbox — see below
```

**Sandbox vs production endpoint:**

| Build environment | `APNS_HOST` | iOS entitlement |
|-------------------|-------------|-----------------|
| Xcode local debug | `https://api.sandbox.push.apple.com` | `aps-environment=development` |
| TestFlight / App Store | `https://api.push.apple.com` | `aps-environment=production` |

If you mix them: APNs returns `400 BadDeviceToken` and pushes silently fail.

### Step 5.4 — Install systemd service

```bash
scp dsiprouter-deploy/voip-push.service root@sbc.myline.tel:/etc/systemd/system/
systemctl daemon-reload
systemctl enable --now voip-push
systemctl status voip-push
```

Expected:
```
● voip-push.service - MyLine VoIP Push Server
     Active: active (running)
     Main PID: <pid> (python3)
```

### Step 5.5 — Test the push server directly

```bash
# On the SBC — needs a real device push token to actually deliver.
# But you can test the auth path with a dummy token (will get BadDeviceToken back):
curl -sX POST http://127.0.0.1:8070/push \
  -H 'Content-Type: application/json' \
  -d '{
    "secret": "YOUR_PUSH_SECRET",
    "token":  "fakedevicetokenfortesting",
    "caller": "+13055551234",
    "caller_name": "Test"
  }'
```

Expected:
```json
{"status":"sent","apns":"{\"reason\":\"BadDeviceToken\"}\n400"}
```

That means: the JWT signed correctly, request reached Apple, Apple rejected the fake token. **The push server is working.** When you send with a real iOS-provided token, you should get `200` instead of `400`.

```bash
# Check service log:
journalctl -u voip-push -f
```

### Step 5.6 — Verify the kamailio → push wire

Kamailio's `route[SENDPUSH]` calls `http_async_query("http://127.0.0.1:8070/push", "PUSH_CB")`. Confirm the URL matches:

```bash
grep -n 'http_async_query.*push' /etc/kamailio/kamailio.cfg
# Expected:  http_async_query("http://127.0.0.1:8070/push", "PUSH_CB");
```

If the URL is different (different port, IPv6, etc.), edit either the Python service or the kamailio config so they match.

---

## Part 6 — dSIPRouter kamailio.cfg push routes

Everything in this section is in `/etc/kamailio/kamailio.cfg` on the SBC. These routes implement the suspend-and-push-resume pattern.

> **Detailed source for every block** — see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) §7. This iOS guide gives a brief overview to help you locate each piece.

### Step 6.1 — REGISTER handler: extract pn-prid + Contact rewrite

In `route[REGISTRAR]` inside the `FLT_DOMAINROUTING && !FLT_EXTERNAL_AUTH` block:

```cfg
# Extract iOS APNs push token from Contact URI parameter
$var(pn_prid) = $(hdr(Contact){re.subst,/.*pn-prid=([a-f0-9A-F]+)[;> ].*/\1/});
if (!strempty($var(pn_prid))) {
    $sht(pushtok=>$tU@$td) = $var(pn_prid);
    sql_query("kam", "INSERT INTO dsip_push_tokens (account, push_token) VALUES ('$tU@$td', '$var(pn_prid)') ON DUPLICATE KEY UPDATE push_token='$var(pn_prid)', updated_at=NOW()");
    xlog("L_INFO", "[REGISTER] Stored APNs token for $tU@$td\n");
}

# Rewrite Contact to a stable SBC address before forwarding to FusionPBX
remove_hf("Contact");
append_hf("Contact: <sip:$tU@sbc.myline.tel:5060>;expires=86400\r\n");
remove_hf("Expires");
append_hf("Expires: 86400\r\n");
```

### Step 6.2 — `dsip_push_tokens` table

```sql
CREATE TABLE IF NOT EXISTS dsip_push_tokens (
    account     VARCHAR(128) NOT NULL,
    push_token  VARCHAR(512) NOT NULL,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (account)
);
```

And the corresponding kamailio htable modparam (around line 944):
```cfg
modparam("htable", "htable", "pushtok=>size=8;autoexpire=86400;dbtable=dsip_push_tokens;cols='account,push_token';")
```

### Step 6.3 — `route[SENDPUSH]`

```cfg
route[SENDPUSH] {
    $var(reg_domain) = $sht(pass_thru_auth=>$si);
    if (!strempty($var(reg_domain))) {
        $var(push_aor) = $tU + "@" + $var(reg_domain);
    } else {
        $var(push_aor) = $tU + "@" + $td;
    }

    $var(pn_token) = $sht(pushtok=>$var(push_aor));
    if ($var(pn_token) == $null) { return; }

    xlog("L_INFO", "[SENDPUSH] APNs push to $var(push_aor) token=$(var(pn_token){s.substr,0,8})... caller=$fU\n");
    $var(http_body) = '{"secret":"YOUR_PUSH_SECRET","token":"' + $var(pn_token) + '","caller":"' + $fU + '","caller_name":"' + $fn + '"}';
    http_async_query("http://127.0.0.1:8070/push", "PUSH_CB");
}

route[PUSH_CB] {
    xlog("L_INFO", "[PUSH_CB] APNs response: ok=$http_ok body=$http_rb\n");
}
```

`PUSH_SECRET` here MUST equal `PUSH_SECRET` in `/opt/myline/voip_push_server.py`.

### Step 6.4 — `route[SUSPEND]`

Stores the in-flight INVITE transaction ID in an htable so a future REGISTER (triggered by the push wakeup) can resume it.

```cfg
route[SUSPEND] {
    $var(join_aor) = $tU + "@" + $sht(pass_thru_auth=>$si);
    $sht(push=>join::$var(join_aor)) = "" + $T(id_index) + ":" + $T(id_label);
    t_suspend();
}
```

### Step 6.5 — `failure_route[PUSH_FAIL]`

Fires if the INVITE was sent (NAT alive) but device didn't respond in 5 sec — assume suspended, send push and re-suspend.

```cfg
failure_route[PUSH_FAIL] {
    if (t_check_status("408|480|503") || !t_any_replied()) {
        $var(pf_join_aor) = $tU + "@" + $sht(pass_thru_auth=>$si);
        $sht(push=>join::$var(pf_join_aor)) = "" + $T(id_index) + ":" + $T(id_label);
        route(SENDPUSH);
        exit;
    }
}
```

### Step 6.6 — `route[RESUME]` and `route[JOIN]`

When the iPhone re-registers post-push, REGISTER handler fires `route(JOIN)` which calls `t_continue()` to resume the original INVITE in `route[RESUME]`. RESUME does the location lookup and relays the INVITE to the now-alive iPhone.

```cfg
route[JOIN] {
    if ($sht(push=>join::$tU@$td) != $null) {
        $var(tids) = $sht(push=>join::$tU@$td);
        $var(idx) = $(var(tids){s.select,0,:});
        $var(lbl) = $(var(tids){s.select,1,:});
        if (t_continue("$var(idx)", "$var(lbl)", "RESUME")) {
            $sht(push=>join::$tU@$td) = $null;
        }
    }
}

route[RESUME] {
    $var(resume_aor) = $rU + "@" + $sht(pass_thru_auth=>$si);
    if (!lookup("location", "sip:$var(resume_aor)")) {
        sl_send_reply("404", "Not Found");
        exit;
    }
    record_route();
    t_relay();
    exit;
}
```

In the REGISTER handler, after `route(RELAY)`:
```cfg
#!ifdef WITH_PUSH
if ($sht(push=>join::$tU@$td) != $null) {
    route(JOIN);
}
#!endif
```

### Step 6.7 — Strip `pn-*` URI parameters before relaying INVITE to iPhone

The iPhone's SIP library silently rejects INVITEs whose Request-URI contains non-standard URI params. Strip them in `route[LOCATION]` for push-capable devices:

```cfg
if (is_method("INVITE") && ($ru =~ "pn-prid=")) {
    t_set_fr(30000, 5000);
    t_on_failure("PUSH_FAIL");

    $ru = $(ru{re.subst,/;pn-provider=[^;>]*//});
    $ru = $(ru{re.subst,/;pn-param=[^;>]*//});
    $ru = $(ru{re.subst,/;pn-prid=[^;>]*//});
}
```

### Step 6.8 — In-dialog BYE relay (iOS hangup quirk fix)

iPhone's SIP library doesn't always include a Route header on BYE. Without this fix, dSIPRouter returns `481 Call/Transaction Does Not Exist` and the BYE never reaches FusionPBX → other party stays connected.

In `route[WITHINDLG]` after the existing `is_method("ACK|UPDATE|INVITE|BYE|PRACK")` block:

```cfg
if (has_totag() && is_method("BYE|UPDATE|INVITE|PRACK")) {
    xlog("L_INFO", "[WITHINDLG] in-dialog $rm without route, relaying to $ru\n");
    route(RELAY);
    exit;
}
```

### Step 6.9 — Sanity check: don't require Content-Length

iPhone SIP library doesn't always include `Content-Length` on UDP. Default `sanity_check("1511","7")` rejects them. Change to `1479` (= 1511 - 32):

```cfg
if (!sanity_check("1479", "7")) {
    xlog("L_WARN", "Malformed SIP message from source address $si:$sp\n");
    exit;
}
```

### Step 6.10 — Aggressive NAT keepalive (every 20s)

```cfg
modparam("nathelper", "natping_interval", 20)
modparam("nathelper", "ping_nated_only", 1)
modparam("nathelper", "sipping_bflag", FLB_NATSIPPING)
modparam("nathelper", "sipping_from", "sip:pinger@UAC_REG_ADDR")
```

20 sec gives margin against typical 30 sec NAT UDP binding timeouts. Combined with the iPhone's own 15 sec OPTIONS, NAT stays open even when the app is briefly backgrounded.

### Step 6.11 — Validate kamailio config and reload

After applying ALL of Part 6:

```bash
kamailio -c /etc/kamailio/kamailio.cfg 2>&1 | grep -iE 'error|syntax'
systemctl restart kamailio
systemctl status kamailio
```

---

## Part 7 — TestFlight & App Store production

### Step 7.1 — Switch to production push environment

Before you submit to TestFlight or App Store, change ONE letter in `MyLineSoftphone.entitlements`:

```xml
<key>aps-environment</key>
<string>production</string>     <!-- was: development -->
```

And ONE line in `/opt/myline/voip_push_server.py` on the SBC:

```python
APNS_HOST = 'https://api.push.apple.com'   # production
```

If you run multiple environments (e.g. staging dSIPRouter + production dSIPRouter), one runs sandbox and the other runs production. Same `.p8` key works for both — APNs differentiates by endpoint.

### Step 7.2 — App Store Connect record

1. https://appstoreconnect.apple.com/apps → **+ New App**
2. Bundle ID: pick the one from Part 1.2
3. SKU: any unique string for your records (e.g. `mylinetelecom-ios-001`)
4. App information: name, category (Business), age rating, privacy policy URL, support URL
5. **App Privacy** — declare what data the app collects (PII: phone numbers, microphone audio, contacts)
6. Click **Prepare for Submission** when first build appears in TestFlight

### Step 7.3 — TestFlight

After Codemagic uploads a build:
1. App Store Connect → My App → **TestFlight**
2. Builds appear under **iOS** after Apple processing (~5-30 min)
3. Add **Internal Testers** (up to 100, no review needed) — distribute via TestFlight app
4. For external testers (up to 10,000), the build needs **App Review** (~24-48h first time, often <12h after)

---

## Part 8 — Verification

### Step 8.1 — Push token registered

After the iPhone registers (open the app):

```bash
# On dSIPRouter:
mysql -u root kamailio -e "SELECT account, LEFT(push_token,16) AS token, updated_at FROM dsip_push_tokens;"
```

Expected: one row per registered iPhone with a non-empty 64-char hex token. If empty, the iOS app didn't include `pn-prid=` in its Contact (see Part 3.1).

### Step 8.2 — Push to a known-good token

```bash
# Get a real token:
TOKEN=$(mysql -u root kamailio -sse "SELECT push_token FROM dsip_push_tokens LIMIT 1;")

curl -sX POST http://127.0.0.1:8070/push \
  -H 'Content-Type: application/json' \
  -d "{\"secret\":\"YOUR_PUSH_SECRET\",\"token\":\"$TOKEN\",\"caller\":\"+13055551234\",\"caller_name\":\"Test\"}"
```

Expected APNs response: `200` (HTTP body empty on success). The iPhone's CallKit screen should appear within 1–2 seconds.

### Step 8.3 — Full inbound call through push

1. Force-quit the iOS app on the iPhone
2. Wait 30 sec for iOS to fully suspend it
3. From another phone, dial the iPhone's number

Watch dSIPRouter:
```bash
journalctl -u kamailio --since "1 minute ago" | grep -iE "SENDPUSH|SUSPEND|JOIN|RESUME|push.*token"
```

Expected sequence:
```
[LOCATION] push-capable device, short timer for 231@76.203.175.9
[SENDPUSH] APNs push to 231@mltpbx.myline.tel token=478444f2... caller=13059684280
suspended transaction [...] 13059684280 => 231@mltpbx.myline.tel
[REGISTER] [PUSH] about to un-suspend transaction for 231@mltpbx.myline.tel
[JOIN] resuming suspended transaction for 231@mltpbx.myline.tel
resuming transaction
```

iPhone CallKit screen appears, the call connects when answered. Audio works both ways.

### Step 8.4 — Outbound call

From the iPhone, dial out. Confirm in FusionPBX CDR that the call shows:
- `direction: outbound` (NOT inbound)
- `context: <customer-domain>` (NOT public)
- `sip_acl_authed_by: trusted_sbc`

If you see "public" context or "providers" acl, the SBC isn't classified correctly in FusionPBX — see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) Part 9 (trusted_sbc ACL).

---

## Part 9 — Troubleshooting

### Apple side

| Symptom | Cause | Fix |
|---------|-------|-----|
| Build fails: "No matching provisioning profile" | Bundle ID mismatch or profile missing capabilities | Re-create profile in Apple Dev portal, re-download |
| TestFlight upload fails: "Missing push notification entitlement" | Entitlements file not included in build | Verify Xcode → Signing & Capabilities shows Push Notifications |
| Build hangs on "Verifying app" in App Store Connect | Apple processing — sometimes takes 30 min | Wait or check [system status](https://developer.apple.com/system-status/) |

### Push notifications

| Symptom | Cause | Fix |
|---------|-------|-----|
| `dsip_push_tokens` table empty | iPhone Contact doesn't have `pn-prid=` | Check `softphone-ios/MyLineSoftphone/SIP/SipHandler.swift` REGISTER builder |
| APNs returns `400 BadDeviceToken` | Sandbox/production mismatch | Match `aps-environment` (entitlements) with `APNS_HOST` (push server) |
| APNs returns `403 InvalidProviderToken` | Wrong Key ID / Team ID / Bundle ID in push server | Re-check Part 5.3 |
| APNs returns `403 ExpiredProviderToken` | JWT older than 1 hour (we always sign fresh — shouldn't happen) | Restart the Python service to clear any state |
| APNs returns `410 Unregistered` | iPhone uninstalled the app or token rotated | Token will refresh on next REGISTER — clear stale tokens periodically |
| Push delivered but iPhone doesn't ring | iOS-side bug in PushKit handler | See bug B fix in commit `44c431e` (build 60+) |
| iPhone never re-registers after push | `sipHandler.start()` not called fast enough | Check Xcode console for `pushRegistry(_:didReceiveIncomingPushWith:)` log |

### NAT / connectivity

| Symptom | Cause | Fix |
|---------|-------|-----|
| First inbound call doesn't ring (after >30 sec idle) | NAT binding expired | Verify natping = 20 sec (Part 6.10), iPhone OPTIONS = 15 sec |
| `Connection reset by peer` floods kamailio log | Old TCP socket from iPhone crashing | Harmless if iPhone is on UDP — confirm `Contact: ;transport=udp` |
| Outbound INVITE from iPhone sanity-rejected | Missing Content-Length on UDP INVITE | Apply Part 6.9 fix |

### Hangup

| Symptom | Cause | Fix |
|---------|-------|-----|
| Iphone hangup leaves other party connected | iPhone BYE without Route → 481 from SBC | Apply Part 6.8 fix |
| Other party hangup leaves iPhone "in call" | iPhone library Call-ID mismatch on BYE | iOS-side fix in commit `44c431e` (bug C — dialog-tag fallback) |

### Audio

| Symptom | Cause | Fix |
|---------|-------|-----|
| Inbound call has no audio (one or both ways) | RTPEngine flags + NAT — see master guide | [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) §7 RTPEngine sections |
| Speaker toggle kills audio | iOS audio session route override timing | iOS app fix needed (bug D — known, deferred) |

---

## Part 10 — File inventory

Source files in this repo → target paths:

| Source (repo) | Target | Owner | Mode |
|---------------|--------|-------|------|
| `softphone-ios/MyLineSoftphone/Info.plist` | embedded in `.ipa` | n/a | n/a |
| `softphone-ios/MyLineSoftphone/MyLineSoftphone.entitlements` | embedded in `.ipa` | n/a | n/a |
| `softphone-ios/MyLineSoftphone/SIP/SipHandler.swift` | embedded in `.ipa` | n/a | n/a |
| `softphone-ios/MyLineSoftphone/Services/SipService.swift` | embedded in `.ipa` | n/a | n/a |
| `dsiprouter-deploy/voip_push_server.py` | `/opt/myline/voip_push_server.py` (dSIPRouter) | root | 755 |
| `dsiprouter-deploy/voip-push.service` | `/etc/systemd/system/voip-push.service` (dSIPRouter) | root | 644 |
| `<your APNs .p8>` | `/etc/myline/AuthKey_XXXX.p8` (dSIPRouter) | root | 600 |
| `codemagic.yaml` (repo root) | committed to git, read by Codemagic CI | n/a | n/a |

Apple-side artifacts (NOT in repo):

| Artifact | Where | Notes |
|----------|-------|-------|
| Apple Developer Account | https://developer.apple.com/account | $99/year |
| App ID `com.yourcompany.softphone` | Apple Dev portal | one per app |
| APNs Auth Key (`.p8`) | Apple Dev → Keys | one per Apple Dev account |
| Apple Distribution certificate (`.p12`) | Apple Dev → Certificates | renew yearly |
| App Store provisioning profile (`.mobileprovision`) | Apple Dev → Profiles | renew yearly |
| App Store Connect API Key (separate `.p8`) | App Store Connect → Users → Keys | for Codemagic upload |
| App Store Connect app record | App Store Connect → My Apps | one per app |

---

## Appendix A — Quick reference

**iOS environment matrix:**

| | aps-environment | APNS_HOST in push server | Build channel |
|-|----------------|--------------------------|---------------|
| Local Xcode debug | `development` | `https://api.sandbox.push.apple.com` | Cmd+R from Xcode |
| TestFlight / App Store | `production` | `https://api.push.apple.com` | Codemagic |

**Key constants that MUST match across components:**

| Constant | Where it lives |
|----------|----------------|
| Bundle Identifier | Apple App ID, Xcode target, push server `APNS_BUNDLE_ID` |
| APNs Key ID | `.p8` file name, push server `APNS_KEY_ID` |
| Team ID | Apple Dev account, push server `APNS_TEAM_ID` |
| Push Secret | push server `PUSH_SECRET`, kamailio `route[SENDPUSH]` body |
| Push port | push server `LISTEN_PORT` (8070), kamailio `http_async_query` URL |

**SBC IP that MUST be allowlisted in FusionPBX:**

| Where | What |
|-------|------|
| FusionPBX `Access Controls` → `trusted_sbc` ACL | dSIPRouter public IP (e.g. `149.28.109.210/32`) |
| Apply via | `apply-inbound-acl=trusted_sbc` in `/etc/freeswitch/sip_profiles/internal.xml` |

---

*End of iOS deployment guide. For platform-wide setup see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md). For WhatsApp-specific deployment see [`whatsapp-deployment-guide.md`](whatsapp-deployment-guide.md).*

*Last updated: 2026-05-15.*
