# WhatsApp Deployment Guide — My Line Telecom

**Version:** 1.0
**Updated:** 2026-05-15
**Scope:** Complete step-by-step setup of WhatsApp Business (text **and** voice) for the My Line Telecom softphone platform.
**Audience:** Engineer deploying a fresh FusionPBX or onboarding a new customer to WhatsApp.

This guide covers everything WhatsApp-specific:

- Part 1 — Meta Business Manager account setup and phone-number registration
- Part 2 — FusionPBX `app/whatsapp/` PHP module **and the nginx location block** (custom code in this repo)
- Part 3 — FusionPBX SMS bridge that forks WhatsApp messages to the module
- Part 4 — dSIPRouter SBC configuration for WhatsApp **voice** calls
- Part 5 — Per-tenant onboarding checklist
- Part 6 — Verification, troubleshooting, file inventory

For the other parts of the platform (SMS only, iOS push, SBC ACL, etc.) see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md).

---

## Architecture overview

```
                    ┌─────────────────────────┐
                    │   iOS softphone         │
                    │  MyLineTelecom-iOS/1.0  │
                    └────────────┬────────────┘
                                 │ SIP MESSAGE / INVITE
                                 ▼
                  ┌──────────────────────────────┐
                  │  dSIPRouter SBC              │
                  │  (kamailio.cfg WhatsApp      │
                  │   routing + Meta auth)       │
                  └──────────────┬───────────────┘
                                 │
                  ┌──────────────┴─────────────────────┐
                  │                                    │
                  ▼                                    ▼
   ┌─────────────────────────────┐       ┌─────────────────────┐
   │  FusionPBX                  │       │  Meta SIP gateway   │
   │                             │       │  wa.meta.vc:5061    │
   │  /var/www/fusionpbx/        │       │  (TLS, SRTP)        │
   │    app/whatsapp/            │◀──┐   │                     │
   │      send.php  ────────────┼───┼──▶│  Graph API (HTTPS)  │
   │      webhook.php ◀─────────┼───┼───│  graph.facebook.com │
   │      extension_map.conf    │   │   │                     │
   │                             │   │   └─────────────────────┘
   │  /usr/share/freeswitch/     │   │
   │    scripts/app/sms/         │   │
   │      index.lua ────────────┼───┘
   │      send.php (bridge)     │
   └─────────────────────────────┘
```

**Three message paths, two voice paths:**

| Path | What it does | Files involved |
|------|--------------|----------------|
| Text-out | Softphone sends WhatsApp message | iOS app → SIP MESSAGE → `index.lua` → `app/sms/send.php` → `app/whatsapp/send.php` → Meta Graph API |
| Text-in | Customer messages your WhatsApp number | Meta webhook → `app/whatsapp/webhook.php` → `v_messages` + `v_message_queue` → SIP MESSAGE → softphone |
| Mark-read | Auto-acknowledge inbound messages | `webhook.php` → Meta Graph API `status=read` |
| Voice-out | Softphone calls a WhatsApp number | iOS app → SIP INVITE → dSIPRouter → Meta SIP (TLS+SRTP) at `wa.meta.vc:5061` |
| Voice-in | Customer calls your WhatsApp number | Meta SIP → dSIPRouter (with `X-WhatsApp-Call:true`) → FusionPBX → bridge to ext via `extension_map.conf` |

---

## Part 1 — Meta Business Manager setup

Do this **once per WhatsApp Business phone number** you want to use. Each Meta-registered number is a "client" in our `$clients` array (multi-tenant).

### 1.1 — Create a Meta Business Account

1. Go to https://business.facebook.com
2. **Create Account** for the business (only needed once total — not per client)
3. **Business Settings → Business Info** — fill in legal name, address, tax ID

### 1.2 — Add a WhatsApp Business Account (WABA)

1. **Business Settings → Accounts → WhatsApp Accounts → +Add → Create a new account**
2. Choose business display name, time zone, category
3. Each WABA can hold up to 25 phone numbers but you typically use one WABA per business

### 1.3 — Add a phone number to the WABA

For each DID you want to expose on WhatsApp:

1. **WhatsApp Manager → Phone Numbers → +Add phone number**
2. Enter the DID in international format (e.g. `+13053561411`)
3. **Verify by SMS** (Meta texts you a code) **or by voice** (Meta calls — useful if the DID doesn't accept SMS)
4. Set display name (shows in WhatsApp chats — must NOT contain "WhatsApp")
5. Save → the number is now registered

After verification, click on the phone number to see:
- **Phone Number ID** — a numeric string (16 digits typical). **Save this** — you'll need it.

### 1.4 — Generate a permanent access token

User access tokens expire after 24 hours. Use a **System User** token instead — those don't expire.

1. **Business Settings → Users → System Users → +Add System User**
2. Name: `WhatsApp Production API`
3. Role: **Admin**
4. **Add Assets**:
   - WhatsApp Account → your WABA → permission: **Manage WhatsApp business account**
   - Phone Number → permission: **Send messages**
5. **Generate New Token**
   - App: choose the App that exposes Meta APIs (create one in https://developers.facebook.com/apps if you don't have one yet)
   - Token Expiration: **Never**
   - Permissions: `whatsapp_business_messaging`, `whatsapp_business_management`
6. **Copy the token immediately** — you only see it once. Format: `EAAxxxxxxxxxxxxx...` (~200 chars)
7. Store securely (do NOT commit to git)

### 1.5 — Register your number for messaging

Some Meta tenants require an explicit "register" call before Cloud API can send messages. Run this from a shell on your FusionPBX server (or anywhere with curl):

```bash
curl -X POST "https://graph.facebook.com/v23.0/<PHONE_NUMBER_ID>/register" \
  -H "Authorization: Bearer <YOUR_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"messaging_product":"whatsapp","pin":"123456"}'
```

If you get error code `133010`, your number isn't registered yet — this command fixes it. The PIN is a 6-digit value you choose; Meta uses it for two-factor checks on phone migration.

### 1.6 — Submit message templates

WhatsApp requires **approved templates** for the first message you send a customer (or after the 24-hour conversation window expires). Without one, Meta silently rejects free-form messages with error code 131047.

1. **WhatsApp Manager → Message Templates → +Create**
2. Choose **Category**:
   - **Utility** — order/appointment updates (cheaper)
   - **Marketing** — promotional content (more expensive, opt-in required)
   - **Authentication** — OTP / verification codes (cheapest)
3. **Body**: write the template text. Use `{{1}}`, `{{2}}` for placeholders that vary per send.
4. **Submit for review** — Meta takes 5 minutes to 24 hours to approve.

**Recommended templates for telecom use:**

| Template name | Category | Suggested body |
|---------------|----------|----------------|
| `initial_contact` | Utility | `Hello! This is {{1}}. We've received your contact information and will follow up shortly. Reply STOP to opt out.` |
| `call_permission` | Utility | `Hi {{1}}, may we call you regarding your account?` — used with `--type=voice_call` to add a "Call" button |
| `appointment_reminder` | Utility | `Reminder: your appointment with {{1}} is on {{2}} at {{3}}.` |

The iOS softphone will send a template body like `__TEMPLATE__:initial_contact:en` when starting a new conversation (no prior inbound from peer).

### 1.7 — Configure the inbound webhook

In **WhatsApp Manager → Configuration → Webhook**:

- **Callback URL:** `https://<your-fusionpbx-host>/app/whatsapp/webhook.php`
  (FusionPBX must be reachable from the public internet via HTTPS with a valid certificate.)
- **Verify Token:** pick a random string (e.g. `openssl rand -hex 32`). You'll paste this into the `$verify_token` variable inside `webhook.php`.
- **Webhook fields → Subscribe to:**
  - `messages` ← required for inbound text
  - `message_status` ← optional, for delivery / read receipts

When you click **Verify and Save**, Meta sends a GET request to your URL with `hub_mode=subscribe&hub_verify_token=...&hub_challenge=...`. The PHP returns the challenge if the token matches. If you see "Webhook verified" success in Meta dashboard, it's wired correctly.

### 1.8 — Enable Voice Calling (Cloud API) — if you want WhatsApp voice calls

This is a separate Meta feature, recently rolled out:

1. **WhatsApp Manager → Account Tools → Calling** (or **Phone Numbers → click number → Calling**)
2. **Enable Calling**
3. Choose **Routing**:
   - **App** — calls show in your customer's Meta app
   - **SIP** — Meta forwards calls to your SIP gateway → choose this
4. **SIP Configuration:**
   - SIP server: `<your-sbc-fqdn>:5060` (e.g. `sbc.myline.tel:5060`)
   - Authentication: Meta will display the auth credentials Meta will use to authenticate **outbound** calls from itself. **Save these** — they go into dSIPRouter `uacreg` (Part 4.3).
5. **Calling permissions** — accept Meta's calling policy (outbound calls are blocked to US, Canada, Egypt, Vietnam, Nigeria; inbound from those countries is fine).

After this, both inbound and outbound voice calls to/from this WhatsApp number go through your SBC.

---

## Part 2 — FusionPBX `app/whatsapp/` module

The custom WhatsApp module lives at `/var/www/fusionpbx/app/whatsapp/` on the FusionPBX server. It is NOT a standard FusionPBX app — it's a 2-file PHP module specific to this deployment.

Source files in this repo: [`fusionpbx-deploy/app/whatsapp/`](../fusionpbx-deploy/app/whatsapp/)

### 2.1 — Deploy the files

```bash
# On the FusionPBX server:
mkdir -p /var/www/fusionpbx/app/whatsapp
cd /var/www/fusionpbx/app/whatsapp

# Copy from the repo (scp from your laptop):
scp -P 7222 fusionpbx-deploy/app/whatsapp/send.php       root@<fusionpbx>:/var/www/fusionpbx/app/whatsapp/
scp -P 7222 fusionpbx-deploy/app/whatsapp/webhook.php    root@<fusionpbx>:/var/www/fusionpbx/app/whatsapp/

# Permissions:
chown www-data:www-data /var/www/fusionpbx/app/whatsapp/*.php
chmod 644 /var/www/fusionpbx/app/whatsapp/*.php
```

### 2.2 — Configure `send.php`

Edit `/var/www/fusionpbx/app/whatsapp/send.php` and fill in the `$clients` array. For each WhatsApp Business number you onboarded in Part 1:

```php
$clients = [
    '13053561411' => [                              // ← phone number, no '+'
        'name'     => 'My Line Tel',                // ← display name (free-form)
        'phone_id' => '1234567890123456',           // ← from Part 1.3
        'token'    => 'EAAxxxxxxxx...',             // ← from Part 1.4
    ],
    '17863053838' => [
        'name'     => 'Network Move',
        'phone_id' => '9876543210987654',
        'token'    => 'EAAxxxxxxxx...',
    ],
    // … add one entry per client number you onboard …
];
```

### 2.3 — Configure `webhook.php`

Edit `/var/www/fusionpbx/app/whatsapp/webhook.php`:

```php
// Must match what you entered in Meta dashboard (Part 1.7)
$verify_token = 'your_random_verify_token';

// From /etc/fusionpbx/config.conf:
$db_pass = 'your_fusionpbx_db_password';

// Find your SMS provider UUID:
//   psql -U fusionpbx -d fusionpbx -c \
//     "SELECT message_provider_uuid, message_provider_name FROM v_message_providers;"
$sms_provider_uuid = 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx';

// Same $clients array as send.php — CRITICAL: keep these in sync
$clients = [
    '13053561411' => [
        'name'     => 'My Line Tel',
        'phone_id' => '1234567890123456',
        'token'    => 'EAAxxxxxxxx...',
    ],
    // …
];

// Default fallback (used if the receiving number isn't in $clients)
$default_token    = 'EAAxxxxxxxx...';
$default_phone_id = '1234567890123456';
```

> **Why two copies of `$clients`?** Because `send.php` is invoked from CLI without web context, while `webhook.php` runs under the Apache user. Sharing one config file across both is cleaner but the current setup duplicates them. Keep them in sync — if you onboard a new number, edit BOTH files.
>
> **Future improvement:** extract the `$clients` array into `/etc/myline/whatsapp_clients.php` and `require` it from both files.

### 2.4 — Add the nginx location block for the webhook

The webhook endpoint at `/app/whatsapp/webhook.php` is NOT served by FusionPBX's default nginx rewrite rules — those route every URL through the FusionPBX `index.php` front controller, which would never run our `webhook.php`. You must add a dedicated `location` block that routes `/app/whatsapp/` directly to PHP-FPM.

Source: [`fusionpbx-deploy/nginx/whatsapp-webhook.conf`](../fusionpbx-deploy/nginx/whatsapp-webhook.conf)

Edit `/etc/nginx/sites-available/fusionpbx` (which is symlinked from `sites-enabled/`). Find the **HTTPS server block** (`server { listen 443; ... }`) and add this INSIDE it, after `server_name` and before the catch-all `location /` block:

```nginx
#whatsapp webhook
location /app/whatsapp/ {
    alias /var/www/fusionpbx/app/whatsapp/;
    index webhook.php;
    location ~ \.php$ {
        fastcgi_pass unix:/var/run/php/php-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $request_filename;
    }
}
```

**What each line does:**

| Directive | Purpose |
|-----------|---------|
| `location /app/whatsapp/` | Catch all requests under `/app/whatsapp/` |
| `alias /var/www/fusionpbx/app/whatsapp/` | Map URL → filesystem path (so `/app/whatsapp/webhook.php` reads `/var/www/fusionpbx/app/whatsapp/webhook.php`) |
| `index webhook.php` | If URL is `/app/whatsapp/` (no file specified), default to `webhook.php` |
| `location ~ \.php$` | Nested location: any `.php` file under here goes to PHP-FPM via fastcgi |
| `fastcgi_pass unix:/var/run/php/php-fpm.sock` | The PHP-FPM Unix socket (path may differ on your distro — check `find /var/run -name 'php*.sock'`) |
| `fastcgi_param SCRIPT_FILENAME $request_filename` | Tell PHP-FPM the actual filesystem path (uses the alias above) |

**Note on PHP-FPM socket path:** the snippet uses `/var/run/php/php-fpm.sock`. On some Debian/Ubuntu installs it's versioned, e.g. `/var/run/php/php8.2-fpm.sock` or `/run/php/php-fpm.sock`. Find yours:

```bash
ls /var/run/php/*.sock 2>/dev/null || find /var/run -name '*fpm*.sock'
```

Then update `fastcgi_pass` accordingly.

**Validate and reload:**

```bash
nginx -t       # syntax check
systemctl reload nginx
```

If `nginx -t` complains, look for misplaced `}` or a missing `;` after `}` of the location block.

### 2.5 — Verify webhook is reachable from Meta

```bash
# From your laptop:
curl -v "https://<fusionpbx-host>/app/whatsapp/webhook.php?hub_mode=subscribe&hub_verify_token=<YOUR_TOKEN>&hub_challenge=test123"
```

Expected response: `200 OK` with body `test123`. If you see:

- `403 Forbidden` → verify token mismatch (check `$verify_token` in `webhook.php`)
- `404 Not Found` → nginx location block missing or wrong (re-do §2.4)
- `502 Bad Gateway` → wrong PHP-FPM socket path in `fastcgi_pass` (see note above)
- `500 Internal Server Error` → check `/var/log/nginx/error.log` and `/var/log/whatsapp_webhook.log`
- HTML response (FusionPBX login page) → location block was added but in the wrong server block, OR nginx fell through to the catch-all (location precedence problem — make sure `location /app/whatsapp/` is in the same server block as your default FusionPBX serving)

### 2.6 — Test `send.php` from CLI

Before any softphone integration, send a manual message:

```bash
# Template send (works anytime — opens a 24h conversation window):
php /var/www/fusionpbx/app/whatsapp/send.php \
    --from=13053561411 \
    --to=15551234567 \
    --template=initial_contact \
    --lang=en_US

# Free-form text (only after recipient has messaged you within 24h):
php /var/www/fusionpbx/app/whatsapp/send.php \
    --from=13053561411 \
    --to=15551234567 \
    --message="Hello from FusionPBX!"

# Show configured clients:
php /var/www/fusionpbx/app/whatsapp/send.php --list
```

Watch the log:
```bash
tail -f /var/log/whatsapp_send.log
```

Successful send looks like:
```
[2026-05-15 12:34:56] SENT from=+13053561411 (My Line Tel) to=15551234567 type=template msg_id=wamid.HBgL...
```

Common errors and fixes are in [Part 6 troubleshooting](#part-6--verification--troubleshooting).

---

## Part 3 — FusionPBX SMS bridge (with WhatsApp routing)

The SMS chatplan bridge is shared between SMS and WhatsApp. It uses the recipient's digit count to decide which channel:

- **10 digits** (e.g. `3055551234`) → SMS via Thinq (FusionPBX queue)
- **11 digits with leading 1** (e.g. `13055551234`) → WhatsApp via the module

Source files: [`fusionpbx-deploy/app/sms/`](../fusionpbx-deploy/app/sms/) and `sms_send.php` (at repo root).

### 3.1 — Deploy the SMS bridge

```bash
mkdir -p /usr/share/freeswitch/scripts/app/sms

scp -P 7222 fusionpbx-deploy/app/sms/index.lua  root@<fusionpbx>:/usr/share/freeswitch/scripts/app/sms/index.lua
scp -P 7222 sms_send.php                        root@<fusionpbx>:/usr/share/freeswitch/scripts/app/sms/send.php

chown www-data:www-data /usr/share/freeswitch/scripts/app/sms/*
chmod 644 /usr/share/freeswitch/scripts/app/sms/*
```

### 3.2 — Verify the chatplan dispatches to `app.lua sms`

Check `/etc/freeswitch/chatplan/default.xml` — there should be:

```xml
<context name="default">
  <extension name="general">
    <condition>
      <action application="set" data="skip_global_process=true"/>
      <action application="lua" data="app.lua sms"/>
    </condition>
  </extension>
</context>
```

`app.lua` is the FusionPBX-default dispatcher that finds the `sms` subfolder and runs `index.lua`. If it's missing, install the FusionPBX SMS app:

```bash
apt install fusionpbx-app-sms
# or via FusionPBX UI: Advanced -> Upgrade -> Apps -> install SMS
```

### 3.3 — How the bridge routes WhatsApp

When a softphone sends a SIP MESSAGE to a 7+ digit number, the chatplan invokes `index.lua` with the message. The Lua script:

1. Filters out inbound messages (only short extension numbers count as outbound)
2. Decides channel by digit count
3. Detects `__TEMPLATE__:name:lang` body markers (WhatsApp template requests)
4. Shells out to `send.php` with all parameters URL-encoded

`send.php` then:
- For `channel=whatsapp`: forks directly to `/var/www/fusionpbx/app/whatsapp/send.php` (skips FusionPBX queue — Meta has its own delivery)
- For `channel=sms`: inserts into `v_message_queue` with `message_json='sms_forced'` (prevents Thinq from auto-routing to WhatsApp) and triggers `message_send_outbound.php`

### 3.4 — Test the bridge end-to-end

From a softphone, send a message to an 11-digit number `+1 305 555 1234`. The bridge should fork to WhatsApp. Watch:

```bash
tail -f /tmp/sms_outbound.log
```

Expected log:
```
[SMS] channel='whatsapp' to_raw=13055551234 to=13055551234 template=none
[SMS] ext lookup: domain=<uuid> ext=180 outbound_caller_id=13053561411
[SMS] Extension 180 -> DID 13053561411 -> 13055551234 (channel: whatsapp)
[SMS] WhatsApp free-form from 13053561411 to 13055551234
Sending as: My Line Tel (+13053561411)
Message sent successfully
  From: +13053561411 (My Line Tel)
  To: 13055551234
  Type: text
  Message ID: wamid.HBgL...
```

---

## Part 4 — dSIPRouter SBC for WhatsApp voice

WhatsApp voice calls flow through the dSIPRouter SBC, NOT FusionPBX directly. The SBC handles Meta's SRTP requirement (WhatsApp uses TLS for signaling and SRTP for media) and the per-DID authentication.

This part is dSIPRouter-side. SQL and kamailio.cfg snippets are also documented in [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) §6 and §7.

### 4.1 — Add the WhatsApp Meta endpoint as a Carrier

In the dSIPRouter web UI:

1. **Carrier Groups → +Add**
   - Name: `WhatsApp-Meta`
   - Type: `Carrier` (8)
   - Domain: leave empty
2. Open the group → **+Add Endpoint**:
   - IP / FQDN: `wa.meta.vc`
   - Port: `5061`
   - Transport: `TLS`
   - Strip prefix: `0`

Behind the scenes this writes:
```sql
-- dr_gateways
INSERT INTO dr_gateways (address, attrs, description) VALUES
  ('wa.meta.vc:5061;transport=tls',
   '<gwid>,8,tls,rtp_savp,rtp_avp',
   'name:Meta_Sip,gwgroup:13,addr_id:<addr_id>');

-- dr_gw_lists
INSERT INTO dr_gw_lists (gwlist, description) VALUES
  ('<gwid>', 'name:WhatsApp-Meta,type:8,lb:13');
```

Note the **gwgroup ID** that dSIPRouter assigned (in our production: `13`). Subsequent steps reference this.

### 4.2 — Whitelist Meta's IP ranges

Meta calls FROM many IPs. Add the official CIDR list to dSIPRouter so kamailio accepts inbound INVITEs from Meta:

```sql
USE kamailio;
INSERT INTO address (grp, ip_addr, mask, port, tag) VALUES
  (8, '31.13.24.0',   21, 0, 'meta-whatsapp'),
  (8, '31.13.64.0',   18, 0, 'meta-whatsapp'),
  (8, '45.64.40.0',   22, 0, 'meta-whatsapp'),
  (8, '57.141.0.0',   21, 0, 'meta-whatsapp'),
  (8, '57.141.8.0',   22, 0, 'meta-whatsapp'),
  (8, '57.141.12.0',  23, 0, 'meta-whatsapp'),
  (8, '57.144.0.0',   14, 0, 'meta-whatsapp'),
  (8, '66.220.144.0', 20, 0, 'meta-whatsapp'),
  (8, '69.63.176.0',  20, 0, 'meta-whatsapp'),
  (8, '69.171.224.0', 19, 0, 'meta-whatsapp'),
  (8, '74.119.76.0',  22, 0, 'meta-whatsapp'),
  (8, '102.132.96.0', 20, 0, 'meta-whatsapp'),
  (8, '103.4.96.0',   22, 0, 'meta-whatsapp'),
  (8, '129.134.0.0',  16, 0, 'meta-whatsapp'),
  (8, '147.75.208.0', 20, 0, 'meta-whatsapp'),
  (8, '157.240.0.0',  16, 0, 'meta-whatsapp'),
  (8, '163.70.128.0', 17, 0, 'meta-whatsapp'),
  (8, '163.77.128.0', 17, 0, 'meta-whatsapp'),
  (8, '173.252.64.0', 18, 0, 'meta-whatsapp'),
  (8, '179.60.192.0', 22, 0, 'meta-whatsapp'),
  (8, '185.60.216.0', 22, 0, 'meta-whatsapp'),
  (8, '185.89.216.0', 22, 0, 'meta-whatsapp'),
  (8, '204.15.20.0',  22, 0, 'meta-whatsapp');
```

Then reload:
```bash
kamcmd permissions.addressReload
```

> Meta updates this list occasionally. Refresh quarterly from: https://developers.facebook.com/docs/whatsapp/cloud-api/get-started/whitelist

### 4.3 — Add Meta auth credentials to `uacreg`

Each Meta-registered phone number needs its SIP auth credentials in `uacreg` so dSIPRouter can answer Meta's 401/407 challenges on outbound calls.

The credentials were displayed by Meta in **Part 1.8** (SIP configuration screen).

```sql
USE kamailio;
INSERT INTO uacreg
  (l_uuid, l_username, l_domain, r_username, r_domain, realm,
   auth_username, auth_password, auth_ha1, auth_proxy,
   expires, flags, reg_delay)
VALUES
  (UUID(),
   '13053561411',              -- l_username (your DID, no +)
   'sbc.myline.tel',           -- l_domain (your SBC FQDN)
   '13053561411',              -- r_username (same DID)
   'wa.meta.vc',               -- r_domain (Meta SIP FQDN)
   'wa.meta.vc',               -- realm
   '13053561411',              -- auth_username (from Meta dashboard)
   'YOUR_META_SIP_PASSWORD',   -- auth_password (from Meta dashboard)
   '',
   '',
   3600,
   13,                          -- flags=13 — this MUST match the value
                                -- kamailio MANAGE_FAILURE checks for
   0);
```

Repeat for each Meta-registered DID.

> **Why `flags=13`?** The kamailio config keys off this value to identify "this is a Meta auth row" (see step 4.4). If you change it, you must also change the SQL query in `MANAGE_FAILURE`. We use `13` to match `gwgroup:13` for consistency.

### 4.4 — Verify the kamailio.cfg has Meta routing

In `/etc/kamailio/kamailio.cfg`, the following blocks should already exist (added during initial SBC setup — see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) §7):

**A. Strip leading `+` on outbound calls to Meta** (around line 1238):
```cfg
if ($fu =~ "wa.meta.vc" && $rU =~ "^\+") {
    $rU = $(rU{s.substr,1,0});
    xlog("L_WARN", "DEBUG STRIP: rU after strip=$rU\n");
}
```

**B. Tag inbound WhatsApp calls with a custom header** for FusionPBX dialplan (around line 1249):
```cfg
if ($fu =~ "wa.meta.vc") {
    append_hf("X-WhatsApp-Call: true\r\n");
}
```

**C. Source classification flag** (around line 1845):
```cfg
else if (allow_source_address(FLT_CARRIER)) {
    setbflag(FLB_SRC_CARRIER);
    if ($fu =~ "wa.meta.vc") { setbflag(FLB_SRC_WHATSAPP); }
}
```

**D. RTPEngine flags for inbound WhatsApp** (around line 3614 — in `route[RTPENGINEOFFER]`):
```cfg
# Meta sends offers as RTP/AVP (unencrypted) over its encrypted SIP/TLS leg.
# Transcode to PCMU/PCMA which Meta requires.
else if ($fu =~ "wa.meta.vc") {
    $var(reflags) = "trust-address replace-origin replace-session-connection rtcp-mux-demux ICE=remove transcode-PCMU transcode-PCMA RTP/AVP";
}
```

**E. RTPEngine flags for outbound WhatsApp** (around line 3732 — in `route[RTPENGINEANSWER]`):
```cfg
# Meta expects SRTP (RTP/SAVP) on the answer side.
else if ($fu =~ "wa.meta.vc") {
    $var(reflags) = "trust-address replace-origin replace-session-connection rtcp-mux-demux ICE=remove transcode-PCMU transcode-PCMA RTP/SAVP";
}
```

**F. Meta auth injection on 401/407** (around line 4283 in `failure_route[MANAGE_FAILURE]`):
```cfg
# When Meta challenges outbound INVITE with 401/407, look up the matching
# uacreg row (flags=13) and inject credentials so uac_auth() can answer.
if (t_check_status("401|407") && $dlg_var(dst_gwgroupid) == "13" && strempty($avp(auth_user))) {
    if (sql_query("kam", "SELECT auth_username, auth_password, realm FROM uacreg WHERE flags=13 LIMIT 1", "ra") && $dbr(ra=>rows) > 0) {
        $avp(auth_user)  = $dbr(ra=>[0,0]);
        $avp(auth_pass)  = $dbr(ra=>[0,1]);
        $avp(auth_realm) = $dbr(ra=>[0,2]);
        xlog("L_WARN", "DEBUG: Injected Meta auth: user=$avp(auth_user)\n");
    }
}
```

### 4.5 — Add routing rules for inbound/outbound WhatsApp

```sql
USE kamailio;

-- INBOUND: Meta calls into our DID → forward to FusionPBX (gwlist #15)
INSERT INTO dr_rules (groupid, prefix, gwlist, description) VALUES
  (20000, '13053561411', '#15', 'name:WhatsApp Inbound 13053561411 to FusionPBX'),
  (20000, '17863053838', '#15', 'name:WhatsApp Inbound 17863053838 to FusionPBX');

-- OUTBOUND: anything routed through groupid 8000 → WhatsApp Meta (gwlist #13)
INSERT INTO dr_rules (groupid, prefix, gwlist, description) VALUES
  (8000, '', '#13', 'name:Meta_outbound');
```

The `#13` references `dr_gw_lists.id=13` (the `WhatsApp-Meta` group from step 4.1). The `#15` references the FusionPBX endpoint group ID (`svrpbx01_Meta` — see master guide §6).

Reload routing:
```bash
kamcmd drouting.reload
```

### 4.6 — Configure FusionPBX to route WhatsApp inbound

When an inbound WhatsApp call arrives, dSIPRouter forwards an INVITE to FusionPBX with `X-WhatsApp-Call: true`. You need a dialplan rule that picks this up and routes to the right extension.

**FusionPBX UI → Dialplan → Dialplan Manager → +Add:**

| Field | Value |
|-------|-------|
| Name | `whatsapp_inbound` |
| Context | `public` |
| Order | `50` |
| Enabled | `true` |
| Condition: `${sip_h_X-WhatsApp-Call}` | `^true$` |
| Condition: `destination_number` | `^(\d{10,11})$` |
| Action: `transfer` | `<extension> XML ${sip_from_host}` |

For multi-tenant deployments, use `/var/www/fusionpbx/app/whatsapp/extension_map.conf` to map DIDs to extensions and load that mapping in a Lua dialplan action. Or hardcode per-domain if your tenant count is small.

---

## Part 5 — Per-tenant onboarding checklist

For each new customer that wants WhatsApp:

| # | Step | Where | Notes |
|---|------|-------|-------|
| 1 | Verify the customer's DID with Meta | Meta Business Manager (§1.3) | One DID per WhatsApp number |
| 2 | Generate token (or reuse global) | Meta System Users (§1.4) | Tokens can be shared across your WABA |
| 3 | Submit at least one template | Meta Templates (§1.6) | `initial_contact` minimum |
| 4 | Add to `send.php` $clients array | FusionPBX `/var/www/fusionpbx/app/whatsapp/send.php` | Phone number → token + phone_id |
| 5 | Add to `webhook.php` $clients array | FusionPBX `/var/www/fusionpbx/app/whatsapp/webhook.php` | KEEP IN SYNC with send.php |
| 6 | (If using voice) — add uacreg row | dSIPRouter MySQL | `flags=13`, `realm='wa.meta.vc'` |
| 7 | (If using voice) — add dr_rules | dSIPRouter MySQL | Inbound DID → `#15`, outbound → `#13` |
| 8 | (If using voice) — FusionPBX dialplan | `whatsapp_inbound` for the domain | Routes incoming calls to extension |
| 9 | Set extension's Outbound Caller ID Number = DID | FusionPBX → Accounts → Extensions | Required so `send.php` knows which "From" to use |
| 10 | Test inbound text via webhook | Send a message to the DID | Check `/var/log/whatsapp_webhook.log` |
| 11 | Test outbound text via softphone | Send to recipient who messaged in | Check `/var/log/whatsapp_send.log` |
| 12 | Test outbound template | Send `__TEMPLATE__:initial_contact:en` body | Verifies template flow |
| 13 | (If voice) — test inbound call | Customer calls DID | Should ring softphone with `X-WhatsApp-Call:true` header |
| 14 | (If voice) — test outbound call | Softphone calls a WhatsApp user | Should auth via uacreg, connect with SRTP audio |

---

## Part 6 — Verification + Troubleshooting

### 6.1 — End-to-end verification

| Test | Steps | Expected |
|------|-------|----------|
| Webhook reachable | `curl https://<host>/app/whatsapp/webhook.php?hub_mode=subscribe&hub_verify_token=<TOKEN>&hub_challenge=test` | Returns `test` with 200 |
| Send template | `php send.php --from=<DID> --to=<recipient> --template=initial_contact` | Recipient sees template message; `whatsapp_send.log` shows SENT |
| Send free-form | Same as above with `--message="hi"` (recipient must have messaged within 24h) | Same as above |
| Receive inbound | Customer sends "test" to your WhatsApp number | `whatsapp_webhook.log` shows received; row in `v_messages`; SIP MESSAGE delivered to registered softphone |
| Outbound voice (if configured) | Softphone dials customer | Customer's WhatsApp rings; audio works both ways (SRTP) |
| Inbound voice | Customer initiates call from WhatsApp | Softphone rings with caller showing X-WhatsApp-Call header |

### 6.2 — Common errors

| Error / symptom | Cause | Fix |
|-----------------|-------|-----|
| Webhook 403 | Verify token mismatch | Re-check `$verify_token` in `webhook.php` matches Meta dashboard |
| Outbound error 131047 | "Outside conversation window" | Send a template first to re-open the 24-hour window |
| Outbound error 133010 | Phone number not registered with Cloud API | Run the `register` curl call in §1.5 |
| Outbound error 190 | Access token expired | Regenerate via Meta System Users (§1.4) — use **Never** expiry |
| Outbound error 100 / `(#100) Param messaging_product is required` | Old API version or malformed payload | Verify `messaging_product=whatsapp` in payload — should be set by `send.php` automatically |
| All outbound succeeds in `whatsapp_send.log` but customer never sees | Number not in Meta's `messaging` allowed list (sandbox limitation) | In Meta dashboard, add the recipient's number to allowed list, OR finish business verification to leave sandbox |
| Inbound webhook fires but message doesn't reach softphone | `provider_uuid` wrong or extension has no registration | Check `SELECT * FROM v_message_queue WHERE message_status='waiting' ORDER BY insert_date DESC LIMIT 5;` |
| Inbound webhook never fires | Meta cannot reach your URL | Test HTTPS reachability from outside; check apache/php-fpm logs |
| Inbound voice rings briefly then drops | uacreg credentials wrong | Check `/var/log/kamailio` for "Injected Meta auth" log; verify uacreg password matches Meta dashboard |
| Inbound voice no audio | RTPEngine flag mismatch | Verify §4.4 D/E (RTP/AVP inbound, RTP/SAVP outbound) |
| Outbound voice returns 480 | Meta rejects (number not allowed for calling, or destination country blocked) | Meta blocks calls TO US/Canada/Egypt/Vietnam/Nigeria — verify destination country |

### 6.3 — Log file reference

| File | Written by | Contents |
|------|-----------|----------|
| `/var/log/whatsapp_send.log` | `app/whatsapp/send.php` | One line per outbound send (SENT or FAILED) |
| `/var/log/whatsapp_webhook.log` | `app/whatsapp/webhook.php` | All inbound message + status update events |
| `/tmp/sms_outbound.log` | SMS bridge (`app/sms/send.php`) | Bridge-level routing decisions (which channel, which DID) |
| `journalctl -u kamailio` | dSIPRouter kamailio | SIP signaling, Meta auth injection log lines, RTPEngine flag log |

---

## Part 7 — File inventory

Source files in this repo → target paths on production servers:

| Source (repo) | Target (server) | Owner | Mode |
|---------------|-----------------|-------|------|
| `fusionpbx-deploy/app/whatsapp/send.php` | `/var/www/fusionpbx/app/whatsapp/send.php` | www-data | 644 |
| `fusionpbx-deploy/app/whatsapp/webhook.php` | `/var/www/fusionpbx/app/whatsapp/webhook.php` | www-data | 644 |
| `fusionpbx-deploy/app/whatsapp/extension_map.conf.example` | `/var/www/fusionpbx/app/whatsapp/extension_map.conf` | www-data | 644 |
| `fusionpbx-deploy/app/sms/index.lua` | `/usr/share/freeswitch/scripts/app/sms/index.lua` | www-data | 644 |
| `sms_send.php` (repo root) | `/usr/share/freeswitch/scripts/app/sms/send.php` | www-data | 644 |
| `fusionpbx-deploy/nginx/whatsapp-webhook.conf` | merge into `/etc/nginx/sites-available/fusionpbx` (HTTPS server block) | root | 644 |

Database changes (dSIPRouter MariaDB):

| Table | Rows added | Purpose |
|-------|------------|---------|
| `dr_gateways` | 1 row per WhatsApp Meta endpoint | `wa.meta.vc:5061;transport=tls` |
| `dr_gw_lists` | 1 row (e.g. id=13) | `name:WhatsApp-Meta,type:8,lb:13` |
| `address` | 23 rows | Meta CIDR whitelist (grp=8) |
| `dr_rules` | 1 outbound + N inbound | Inbound `#15`, outbound `#13` |
| `uacreg` | 1 row per Meta-registered DID | `flags=13`, `realm='wa.meta.vc'` |

Kamailio config changes (`/etc/kamailio/kamailio.cfg`): see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md) §7.8–§7.11.

FusionPBX dialplan: 1 rule in `public` context for WhatsApp inbound voice (§4.6).

---

## Appendix A — Quick reference

**Required URLs to bookmark:**
- https://business.facebook.com — Business Manager
- https://developers.facebook.com/apps — Apps + token generation
- https://developers.facebook.com/docs/whatsapp/cloud-api — API docs
- https://developers.facebook.com/docs/whatsapp/cloud-api/get-started/whitelist — Meta IP CIDR list

**Required values per WhatsApp number (collect these for each one you onboard):**

| Item | Where to find | Example |
|------|---------------|---------|
| Phone Number | The DID itself | `13053561411` |
| Display Name | Meta WhatsApp Manager → Phone Numbers | `My Line Telecom` |
| Phone Number ID | Meta WhatsApp Manager → click number → top of page | `1234567890123456` |
| Access Token | Meta Business Settings → System Users → Generate | `EAAxxxxxx...` (~200 chars) |
| SIP auth password (voice) | Meta WhatsApp Manager → Calling → SIP Setup | shown once on enable |

---

*End of WhatsApp deployment guide. For the platform-wide deployment (FusionPBX, dSIPRouter, iOS push, SMS-only), see [`myline-platform-deployment-guide.md`](myline-platform-deployment-guide.md).*

*Last updated: 2026-05-15.*
