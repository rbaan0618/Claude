# My Line Telecom — Complete Platform Deployment Guide

**Version:** 1.0
**Updated:** 2026-05-15
**Audience:** Engineer deploying a fresh server, or replicating the production setup on a new server, or doing a major version upgrade.

This guide is a **step-by-step runbook** covering every server-side change required to make the My Line Telecom softphone work end-to-end across:

- **FusionPBX** (per-customer PBX — multi-tenant)
- **dSIPRouter SBC** (front-door for mobile clients, push trigger, WhatsApp Meta proxy)
- **Meta Cloud API** (WhatsApp Business)
- **Apple APNs** (iOS push wakeup)
- **Thinq** (or other HTTP SMS provider)

Each section is self-contained. If you only need to add SMS to an existing FusionPBX, jump straight to Part 3. If you only need to add a new tenant, jump to Part 7.

---

## Table of contents

- **Part 1** — [Architecture overview](#part-1--architecture-overview)
- **Part 2** — [Prerequisites and assumptions](#part-2--prerequisites-and-assumptions)
- **Part 3** — [FusionPBX: SMS bridge (Lua + PHP)](#part-3--fusionpbx-sms-bridge)
- **Part 4** — [FusionPBX: WhatsApp module setup](#part-4--fusionpbx-whatsapp-module)
- **Part 5** — [Meta Cloud API: WhatsApp Business configuration](#part-5--meta-cloud-api-configuration)
- **Part 6** — [dSIPRouter SBC: database setup](#part-6--dsiprouter-database-setup)
- **Part 7** — [dSIPRouter SBC: kamailio.cfg changes](#part-7--dsiprouter-kamailiocfg-changes)
- **Part 8** — [APNs iOS push: setup and wiring](#part-8--apns-ios-push-setup)
- **Part 9** — [FusionPBX trusted-SBC ACL](#part-9--fusionpbx-trusted-sbc-acl)
- **Part 10** — [Per-tenant configuration checklist](#part-10--per-tenant-checklist)
- **Part 11** — [Verification](#part-11--verification)
- **Part 12** — [Troubleshooting](#part-12--troubleshooting)
- **Part 13** — [File inventory](#part-13--file-inventory)
- **Part 14** — [Upgrade notes](#part-14--upgrade-notes)

---

## Part 1 — Architecture overview

```
                  ┌──────────────────────────┐
                  │   iOS softphone          │
                  │  MyLineTelecom-iOS/1.0   │
                  └─────────────┬────────────┘
                                │ SIP UDP/5090 (REGISTER, INVITE, MESSAGE, OPTIONS)
                                │ + X-Push-Token in Contact ;pn-prid=
                                ▼
              ┌─────────────────────────────────────────┐
              │  dSIPRouter SBC                         │
              │  sbc.myline.tel  (149.28.109.210)       │
              │  ┌───────────────────────────────────┐  │
              │  │ Kamailio                          │  │
              │  │  • REGISTER → rewrite Contact     │  │
              │  │    to sbc.myline.tel + extract    │  │
              │  │    pn-prid → dsip_push_tokens     │  │
              │  │  • INVITE inbound → push trigger  │  │
              │  │  • INVITE → Meta routing rules    │  │
              │  │  • MESSAGE → forward to FusionPBX │  │
              │  │  • MANAGE_FAILURE → Meta auth     │  │
              │  └───────────────────────────────────┘  │
              │  ┌───────────────────────────────────┐  │
              │  │ RTPEngine — media anchor + NAT    │  │
              │  └───────────────────────────────────┘  │
              │  ┌───────────────────────────────────┐  │
              │  │ voip_push.php → APNs HTTP/2       │  │
              │  └───────────────────────────────────┘  │
              └─────────────┬───────────────────────────┘
                            │ SIP UDP/5060
                            ▼
        ┌────────────────────────────────────────────────────┐
        │  FusionPBX  (per-tenant: mltpbx.myline.tel, etc.)  │
        │  ┌────────────────────┐  ┌─────────────────────┐   │
        │  │ FreeSWITCH         │  │ Web admin + PHP     │   │
        │  │ • REGISTER → store │  │ • app/sms/         │   │
        │  │ • INVITE → dialplan│  │ • app/whatsapp/    │   │
        │  │ • MESSAGE chatplan │  │ • CDR + Messages    │   │
        │  └────────────────────┘  └─────────────────────┘   │
        └────────────┬─────────────────────────┬─────────────┘
                     │                         │
                     │ chatplan → app.lua sms  │
                     ▼                         ▼
        ┌───────────────────┐         ┌────────────────────┐
        │ Thinq SMS REST    │         │ ASTPP → carrier    │
        │ (HTTP API)        │         │ (PSTN trunk)       │
        └───────────────────┘         └────────────────────┘
                                                ▲
                                                │ (real PSTN carrier)
                                                │
        ┌──────────────────────────────────────────────────────┐
        │ Meta Cloud API (graph.facebook.com)                  │
        │ ↑ outbound WhatsApp from app/whatsapp/send.php       │
        │ ↓ inbound WhatsApp via webhook (HTTPS)               │
        │ ↑↓ WhatsApp VOICE via dSIPRouter ↔ wa.meta.vc:5061   │
        └──────────────────────────────────────────────────────┘
```

**Five integrations + APNs:**

| # | What | Lives on | Trigger |
|---|------|----------|---------|
| A | Outbound SMS | FusionPBX `app/sms/index.lua` + `send.php` | SIP MESSAGE chatplan |
| B | Outbound WhatsApp (text) | FusionPBX `app/whatsapp/send.php` | SMS bridge detects `X-Channel: whatsapp` and forks |
| C | Inbound WhatsApp (text) | FusionPBX `app/whatsapp/webhook.php` | Meta webhook → DB → softphone via SIP MESSAGE |
| D | WhatsApp **voice** | dSIPRouter `dr_rules` + `dr_gateways` + `uacreg` (Meta auth) | INVITE to/from `wa.meta.vc` |
| E | iOS VoIP push | dSIPRouter `voip_push.php` + `pushtok` htable | INVITE for offline iPhone |

---

## Part 2 — Prerequisites and assumptions

| Item | Value used in this guide |
|------|--------------------------|
| FusionPBX install path | `/var/www/fusionpbx/` |
| FreeSWITCH scripts | `/usr/share/freeswitch/scripts/` |
| FreeSWITCH user/group | `www-data:www-data` (Debian/Ubuntu) |
| FusionPBX DB | PostgreSQL, db name `fusionpbx` |
| FusionPBX config | `/etc/fusionpbx/config.conf` |
| dSIPRouter DB | MariaDB / MySQL, db name `kamailio` |
| dSIPRouter Kamailio config | `/etc/kamailio/kamailio.cfg` |
| dSIPRouter public IP | `149.28.109.210` (replace with your value) |
| SBC FQDN | `sbc.myline.tel` |
| Multi-tenant domain pattern | `customer.myline.tel` |
| SBC SIP listen port | 5060 (UDP) |
| iOS softphone SIP listen port | 5090 (UDP) |
| WhatsApp Meta SIP endpoint | `wa.meta.vc:5061` (TLS, with SRTP/RTP/SAVP) |
| Meta-allowed IPs | Static Meta CIDRs in `address` table (see Part 6) |
| APNs key | `.p8` file at `/etc/myline/AuthKey_XXXX.p8` |
| iOS app bundle | `com.mylinetelecom.softphone` |
| iOS PushKit topic | `com.mylinetelecom.softphone.voip` |

### 2.1 — Initial sanity checks

Before starting any deployment, confirm the servers respond:

```bash
# FusionPBX
psql -U fusionpbx -d fusionpbx -c "SELECT version();"
fs_cli -x "status"

# dSIPRouter
mysql -u root kamailio -e "SELECT 1;"
systemctl status kamailio
systemctl status rtpengine
```

If any of these fail, fix it before continuing.

---

## Part 3 — FusionPBX SMS bridge

When the softphone sends an outbound SMS via SIP MESSAGE, FreeSWITCH routes it through the **chatplan** and calls `/usr/share/freeswitch/scripts/app/sms/index.lua`. Without it, the message is silently dropped (server replies 202 Accepted but nothing happens).

### Step 3.1 — Create the scripts directory

```bash
mkdir -p /usr/share/freeswitch/scripts/app/sms
```

### Step 3.2 — Deploy the Lua bridge

```bash
cat > /usr/share/freeswitch/scripts/app/sms/index.lua << 'EOF'
local from_user = message:getHeader("from_user")
local from_host = message:getHeader("from_host")
local to_user   = message:getHeader("to_user")
local body      = message:getBody()
local channel   = message:getHeader("X-Channel") or ""
local template  = message:getHeader("X-Template") or ""
local lang      = message:getHeader("X-Template-Lang") or "en_US"

freeswitch.consoleLog("INFO", string.format(
    "[SMS] Outbound: %s@%s -> %s channel=%s template=%s\n",
    tostring(from_user), tostring(from_host), tostring(to_user),
    channel, template))

if from_user and to_user and from_host and (body or template ~= "") then
    local function shellesc(s)
        return (s or ""):gsub("'", "'\\''")
    end
    local cmd = string.format(
        "php /usr/share/freeswitch/scripts/app/sms/send.php "
     .. "'from=%s&domain=%s&to=%s&body=%s&channel=%s&template=%s&lang=%s' "
     .. ">> /tmp/sms_outbound.log 2>&1 &",
        shellesc(from_user), shellesc(from_host), shellesc(to_user),
        shellesc(body or ""), shellesc(channel),
        shellesc(template), shellesc(lang)
    )
    os.execute(cmd)
else
    freeswitch.consoleLog("WARNING", string.format(
        "[SMS] Missing params - from:%s to:%s body:%s\n",
        tostring(from_user), tostring(to_user), tostring(body)))
end
EOF
```

### Step 3.3 — Deploy the PHP sender (`send.php`)

The canonical version lives in this repo at the root as `sms_send.php`. Copy it to:

```bash
scp sms_send.php root@fusionpbx-host:/usr/share/freeswitch/scripts/app/sms/send.php
```

What this script does (per its 202 lines):

1. Parses `from`, `domain`, `to`, `body`, `channel`, `template`, `lang` from CLI args
2. Looks up `domain_uuid` in `v_domains` (multi-tenant)
3. Looks up extension's **Outbound Caller ID Number** in `v_extensions` → sender DID
4. **If `channel=whatsapp`**: invokes `/var/www/fusionpbx/app/whatsapp/send.php` directly (bypasses FusionPBX message queue, calls Meta Graph API directly)
5. **Else (SMS path)**: finds `provider_uuid` for the DID in `v_destinations`, inserts row into `v_message_queue` with `message_json='sms_forced'` (prevents Thinq auto-WhatsApp routing for 11-digit numbers), then triggers `message_send_outbound.php`

The script uses **direct PDO** for reads (FusionPBX's database class can be unreliable in CLI without session context) and FusionPBX's `database::save()` only for the queue insertion.

### Step 3.4 — Permissions

```bash
chown www-data:www-data /usr/share/freeswitch/scripts/app/sms/index.lua \
                        /usr/share/freeswitch/scripts/app/sms/send.php
chmod 644 /usr/share/freeswitch/scripts/app/sms/index.lua \
          /usr/share/freeswitch/scripts/app/sms/send.php
```

### Step 3.5 — Verify chatplan dispatch

Check `/etc/freeswitch/chatplan/default.xml`. It should contain:

```xml
<extension name="general">
  <condition>
    <action application="set" data="skip_global_process=true"/>
    <action application="lua" data="app.lua sms"/>
  </condition>
</extension>
```

`app.lua` is the FusionPBX-default dispatcher that finds the `sms` subfolder and runs `index.lua`. If `app.lua` is missing, install the FusionPBX `app/sms/` core package: `apt install fusionpbx-app-sms` (or your distro's equivalent).

### Step 3.6 — Test

```bash
tail -f /tmp/sms_outbound.log
```

From softphone → send SMS to a 10-digit number. Expected:

```
[SMS] channel='auto' to_raw=3055551234 to_digits=3055551234 to=3055551234
[SMS] ext lookup: domain=<uuid> ext=180 outbound_caller_id=13055009011
[SMS] Extension 180 -> DID 13055009011 -> 3055551234 (channel: )
[SMS] Sent via queue <uuid> from 13055009011 to 3055551234
```

---

## Part 4 — FusionPBX WhatsApp module

The WhatsApp module is a separate FusionPBX app that handles both outbound and inbound text. Voice WhatsApp goes through dSIPRouter (Part 6/7), not this module.

### Step 4.1 — Install the app

The `app/whatsapp/` module should be deployed under `/var/www/fusionpbx/app/whatsapp/`. Source it from your private repo or copy from a working install.

Expected file layout:
```
/var/www/fusionpbx/app/whatsapp/
├── app_config.php             # FusionPBX app metadata
├── app_languages.php          # Localization
├── app_menu.php               # Add menu entries
├── send.php                   # CLI: php send.php --from=X --to=Y --message=Z (or --template=N --lang=L)
├── webhook.php                # Meta webhook receiver (public HTTPS endpoint)
├── chat.php                   # Admin UI for conversation history
├── templates.php              # Admin UI for managing approved templates
├── resources/
│   ├── config.php             # Meta token + phone_number_id
│   ├── functions.php          # Meta Graph API helpers
│   └── classes/
│       └── whatsapp.php
└── root.php                   # FusionPBX hook
```

Permissions:
```bash
chown -R www-data:www-data /var/www/fusionpbx/app/whatsapp/
```

### Step 4.2 — Register the app with FusionPBX

```bash
cd /var/www/fusionpbx
sudo -u www-data php /var/www/fusionpbx/core/upgrade/upgrade_schema.php  # registers app menu and permissions
sudo -u www-data php /var/www/fusionpbx/core/upgrade/upgrade_domains.php # applies per-domain
```

Or via the UI: **Advanced → Upgrade → Schema** and then **Domains**.

### Step 4.3 — Configure Meta credentials per-tenant

Edit `/var/www/fusionpbx/app/whatsapp/resources/config.php`:

```php
<?php
return [
    // Global default (override per-domain via domain_settings if needed)
    'meta_token'           => 'EAAxxxxxxxxxxxxx',  // permanent system-user token
    'meta_phone_number_id' => '1234567890',         // from Meta Business Manager
    'meta_business_id'     => '9876543210',
    'webhook_verify_token' => 'pick-a-random-string-32-chars',
    'graph_api_version'    => 'v20.0',
];
```

For multi-tenant, the cleanest pattern is per-domain settings:
```bash
# In FusionPBX UI:
# Advanced → Default Settings → +Add
#   category: whatsapp
#   subcategory: meta_token
#   value: EAAxxxxx
# Then per-domain override in Domain Settings.
```

### Step 4.4 — Webhook URL (Meta dashboard)

In Meta Business Manager:
- **WhatsApp → Configuration → Webhook**
- Callback URL: `https://<fusionpbx-host>/app/whatsapp/webhook.php`
- Verify Token: paste the same value as `webhook_verify_token` from step 4.3
- Subscribed fields: `messages`, `message_status`

The webhook writes inbound messages to a FusionPBX table (typically `v_messages` or a dedicated `v_whatsapp_messages`) and then dispatches an in-cluster SIP MESSAGE to the recipient extension.

---

## Part 5 — Meta Cloud API configuration

This is on the Meta side, not your servers. Follow it once per WhatsApp Business phone number.

### Step 5.1 — Meta Business Manager account

1. Sign up at https://business.facebook.com
2. Create a **Business Account** for My Line Telecom
3. **WhatsApp → Add WhatsApp Account**

### Step 5.2 — Register a phone number with Meta

For each DID you want to use as a WhatsApp sender:
1. **WhatsApp → Phone Numbers → +Add**
2. Choose **Verify via SMS** (or **Call** if SMS not available)
3. Enter the DID's verification code

This phone number now has a **phone-number-id** (a long numeric string). You'll need this for Step 4.3.

### Step 5.3 — Generate a permanent system-user token

Free-tier user tokens expire every 24 hours. For production use:

1. **Business Settings → Users → System Users → +Add System User**
2. Role: **Admin**
3. Assign it to your **WhatsApp Business Account**
4. **Generate Token** → permissions: `whatsapp_business_messaging`, `whatsapp_business_management`
5. **No expiration** → save the token (only shown once)

Paste the token into `config.php` (step 4.3) as `meta_token`.

### Step 5.4 — Submit message templates

For first-contact / outside-the-24h-window messages, Meta requires approved templates:

1. **WhatsApp → Message Templates → +Create**
2. Category (Marketing / Utility / Authentication)
3. Body text — use `{{1}}`, `{{2}}` for variables
4. Submit for approval (5 minutes to 24 hours)

When the iOS softphone sends a template, it includes `X-Template: <template-name>` and `X-Template-Lang: en_US` in the SIP MESSAGE. The Lua bridge forwards these, and `app/whatsapp/send.php --template=<name> --lang=en_US` invokes the Meta Graph API.

### Step 5.5 — Voice call routing (SIP)

WhatsApp **voice calls** use a different mechanism: Meta provides a SIP endpoint at `wa.meta.vc:5061` (TLS + SRTP). To enable voice WhatsApp:

1. **WhatsApp → Settings → Calling → Enable Calling**
2. Choose **SIP routing**
3. Set SIP endpoint: `sbc.myline.tel:5060`
4. Set authentication credentials (will be stored in dSIPRouter `uacreg`, see Part 6.5)

This is the entry point for outbound WhatsApp voice (from softphone → SBC → Meta) and inbound (from Meta → SBC → FusionPBX → softphone).

---

## Part 6 — dSIPRouter database setup

dSIPRouter uses MariaDB/MySQL with database `kamailio`. The setup below uses the **dSIPRouter web UI** wherever possible (UI changes write to the same tables we document here).

> Open the dSIPRouter UI: `http://<sbc-ip>:5000` (default port). Login with the admin credentials from `/etc/dsiprouter/.dsiprouter.cfg`.

### Step 6.1 — Add hosted domains

In dSIPRouter UI: **Domain Routing → +Add Domain**

For each FusionPBX customer:

| Field | Example |
|-------|---------|
| Domain | `mltpbx.myline.tel` |
| DID | `mltpbx.myline.tel` |
| Auth Type | `IP-based` |
| PBX Cluster IP | `140.82.26.232` (FusionPBX) |
| Routing Algorithm | round-robin |

This writes to the `domain` table. Verify:
```bash
mysql -u root kamailio -e "SELECT id, domain, did FROM domain WHERE domain LIKE '%myline.tel%';"
```

### Step 6.2 — Add the FusionPBX as an Endpoint Gateway

**UI: Endpoint Groups → +Add**

| Field | Value |
|-------|-------|
| Name | `svrpbx01` |
| Strip prefix | `0` |
| Domain | `<your domain>` |
| Endpoint IP | `140.82.26.232` (FusionPBX) |
| Port | `5060` |

This creates entries in `dr_gateways` (gwid=58 in our example) and `dr_gw_lists` (lb=12 → `name:SVRPBX01,type:9,lb:12`).

### Step 6.3 — Set up the Meta WhatsApp gateway

The WhatsApp Meta SIP endpoint is `wa.meta.vc:5061` (TLS).

**UI: Carrier Groups → +Add**

| Field | Value |
|-------|-------|
| Name | `WhatsApp-Meta` |
| Type | Carrier (8) |
| Domain | (leave empty — Meta is a carrier, not a domain) |

Then **+Add Endpoint** under this group:

| Field | Value |
|-------|-------|
| IP | `wa.meta.vc` (FQDN, will resolve) |
| Port | `5061` |
| Transport | `TLS` |
| Strip prefix | `0` |
| Prepend | (leave empty) |

This writes:
- `dr_gateways` row: `gwid=59, address='wa.meta.vc:5061;transport=tls', attrs='59,8,tls,rtp_savp,rtp_avp', description='name:Meta_Sip,gwgroup:13,addr_id:79'`
- `dr_gw_lists` row: `id=13, gwlist='59', description='name:WhatsApp-Meta,type:8,lb:13'`

Verify:
```bash
mysql -u root kamailio -e "SELECT gwid, address, attrs FROM dr_gateways WHERE description LIKE '%Meta%';"
```

### Step 6.4 — Whitelist Meta IP ranges

Meta's WhatsApp servers can call us from many IPs. Add the official Meta CIDR ranges to the `address` table with `grp=8` (carrier group):

```sql
-- Run on dSIPRouter MariaDB
USE kamailio;
INSERT INTO address (grp, ip_addr, mask, port, tag) VALUES
(8, '31.13.24.0',    21, 0, 'meta-whatsapp'),
(8, '31.13.64.0',    18, 0, 'meta-whatsapp'),
(8, '45.64.40.0',    22, 0, 'meta-whatsapp'),
(8, '57.141.0.0',    21, 0, 'meta-whatsapp'),
(8, '57.141.8.0',    22, 0, 'meta-whatsapp'),
(8, '57.141.12.0',   23, 0, 'meta-whatsapp'),
(8, '57.144.0.0',    14, 0, 'meta-whatsapp'),
(8, '66.220.144.0',  20, 0, 'meta-whatsapp'),
(8, '69.63.176.0',   20, 0, 'meta-whatsapp'),
(8, '69.171.224.0',  19, 0, 'meta-whatsapp'),
(8, '74.119.76.0',   22, 0, 'meta-whatsapp'),
(8, '102.132.96.0',  20, 0, 'meta-whatsapp'),
(8, '103.4.96.0',    22, 0, 'meta-whatsapp'),
(8, '129.134.0.0',   16, 0, 'meta-whatsapp'),
(8, '147.75.208.0',  20, 0, 'meta-whatsapp'),
(8, '157.240.0.0',   16, 0, 'meta-whatsapp'),
(8, '163.70.128.0',  17, 0, 'meta-whatsapp'),
(8, '163.77.128.0',  17, 0, 'meta-whatsapp'),
(8, '173.252.64.0',  18, 0, 'meta-whatsapp'),
(8, '179.60.192.0',  22, 0, 'meta-whatsapp'),
(8, '185.60.216.0',  22, 0, 'meta-whatsapp'),
(8, '185.89.216.0',  22, 0, 'meta-whatsapp'),
(8, '204.15.20.0',   22, 0, 'meta-whatsapp');

-- Reload permissions module
-- (or from kamailio: kamcmd permissions.addressReload )
```

Then reload kamailio:
```bash
kamcmd permissions.addressReload
```

> **Re-check this list at least once a quarter.** Meta publishes the current ranges at `https://developers.facebook.com/docs/whatsapp/cloud-api/get-started/whitelist`.

### Step 6.5 — Set up UAC registration for Meta auth

Meta requires SIP digest auth on every INVITE. dSIPRouter stores Meta credentials in `uacreg` and the kamailio `MANAGE_FAILURE` route auto-injects them on 401/407.

For each DID you registered with Meta in step 5.2, insert a `uacreg` row:

```sql
USE kamailio;
INSERT INTO uacreg
  (l_uuid, l_username, l_domain, r_username, r_domain, realm,
   auth_username, auth_password, auth_ha1, auth_proxy, expires, flags, reg_delay)
VALUES
  (UUID(), '13053561411', 'sbc.myline.tel', '13053561411', 'wa.meta.vc', 'wa.meta.vc',
   '13053561411', 'YOUR_META_SIP_PASSWORD', '', '', 3600, 13, 0);
```

Repeat for each Meta-registered DID. The **`flags=13` is critical** — the kamailio config keys off this number to identify Meta auth rows (see Part 7.7).

Verify:
```bash
mysql -u root kamailio -e "SELECT id, l_username, r_username, realm, flags FROM uacreg WHERE flags=13;"
```

### Step 6.6 — WhatsApp routing rules (dr_rules)

**Inbound** WhatsApp call (Meta → FusionPBX): match the DID prefix and route to the FusionPBX endpoint group (lb=15 in our example, which is the gw_list named `svrpbx01_Meta`).

```sql
USE kamailio;
INSERT INTO dr_rules (groupid, prefix, gwlist, description) VALUES
  (20000, '13053561411', '#15', 'name:WhatsApp Inbound 13053561411 to FusionPBX'),
  (20000, '17863053838', '#15', 'name:WhatsApp Inbound 17863053838 to FusionPBX');
```

**Outbound** WhatsApp call (softphone via FusionPBX → Meta): route everything via gwgroup 13 (Meta).

```sql
INSERT INTO dr_rules (groupid, prefix, gwlist, description) VALUES
  (8000, '', '#13', 'name:Meta_outbound');
```

> The `#13` and `#15` syntax in `gwlist` references the `dr_gw_lists.id` (i.e. carrier groups).

Reload routing:
```bash
kamcmd drouting.reload
```

### Step 6.7 — Verify the `dsip_push_tokens` table exists

This table stores per-user APNs push tokens (populated by the REGISTER handler — see Part 7.2):

```bash
mysql -u root kamailio -e "DESCRIBE dsip_push_tokens;"
```

Expected schema:
```
account     VARCHAR(128) NOT NULL PRIMARY KEY
push_token  VARCHAR(512) NOT NULL
updated_at  DATETIME      ON UPDATE CURRENT_TIMESTAMP
```

If missing:
```sql
CREATE TABLE dsip_push_tokens (
    account     VARCHAR(128) NOT NULL,
    push_token  VARCHAR(512) NOT NULL,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (account)
);
```

---

## Part 7 — dSIPRouter kamailio.cfg changes

This section documents every change made to `/etc/kamailio/kamailio.cfg` to support My Line Telecom's softphone, iOS push, WhatsApp routing, and FusionPBX integration. Each change is a single, isolated edit — apply them in order on a fresh dSIPRouter install.

> **Before editing**, back up: `cp /etc/kamailio/kamailio.cfg /etc/kamailio/kamailio.cfg.backup-$(date +%F)`
>
> **After all edits**, validate and restart:
> ```bash
> kamailio -c /etc/kamailio/kamailio.cfg 2>&1 | grep -iE 'error|syntax'
> systemctl restart kamailio
> systemctl status kamailio
> ```

### 7.1 — Push token htables

These two htables back the iOS push system. They go alongside the other `htable` modparam lines (around line ~940).

```cfg
# Push token storage: key = "USER@DOMAIN", value = hex push token.
# DB-backed via dsip_push_tokens so tokens survive restart.
# autoexpire=86400 (24h) matches our REGISTER forwarding window to FusionPBX.
modparam("htable", "htable", "pushtok=>size=8;autoexpire=86400;dbtable=dsip_push_tokens;cols='account,push_token';")

# Suspended-INVITE join keys: key = "join::USER@DOMAIN", value = transaction id.
# In-memory only (transient, lifetime of a single ringing INVITE).
modparam("htable", "htable", "push=>size=10;autoexpire=120;")
```

### 7.2 — REGISTER handler: store push token + rewrite Contact for FusionPBX

In `route[REGISTRAR]` inside the `FLT_DOMAINROUTING && !FLT_EXTERNAL_AUTH` block (around line 2090):

```cfg
if (isflagset(FLT_DOMAINROUTING) && !isflagset(FLT_EXTERNAL_AUTH)) {
    # ── Clean up duplicate location entries before save ──
    # The iPhone sends two slightly different Contacts at startup
    # (with and without pn-prid as the push token becomes available);
    # without this DELETE they accumulate and cause INVITE forking.
    # Only delete on non-zero Expires (real registration, not unregister).
    if ($hdr(Expires) != "0") {
        sql_query("kam", "DELETE FROM location WHERE username='$tU' AND domain='$td'");
    }

    $var(save_ret) = save("location", "0x02");
    if ($var(save_ret) < 0) {
        xlog("L_ERR", "[REGISTRAR] save(location) FAILED ret=$var(save_ret) for $tU@$td contact=$ct\n");
        sl_reply_error();
    }

    # ── Extract iOS APNs push token from Contact URI ──
    # iOS app sends: Contact: <sip:USER@ip;...;pn-provider=apns;pn-param=BUNDLE;pn-prid=HEX_TOKEN>
    # Store in htable so route[SENDPUSH] can wake a suspended device.
    $var(pn_prid) = $(hdr(Contact){re.subst,/.*pn-prid=([a-f0-9A-F]+)[;> ].*/\1/});
    if (!strempty($var(pn_prid))) {
        $sht(pushtok=>$tU@$td) = $var(pn_prid);
        sql_query("kam", "INSERT INTO dsip_push_tokens (account, push_token) VALUES ('$tU@$td', '$var(pn_prid)') ON DUPLICATE KEY UPDATE push_token='$var(pn_prid)', updated_at=NOW()");
        xlog("L_INFO", "[REGISTER] Stored APNs token for $tU@$td\n");
    }

    # ── Save the original request domain so we can recover it later ──
    $var(rd_orig) = $rd;

    # ... (existing dispatcher logic stays here) ...

    # ── Pass-through-auth domain mapping ──
    # Save PBX_IP -> domain so that route[LOCATION] can recover the
    # registrar domain when an INVITE arrives from FusionPBX.
    if (!is_ip($(avp(domain_pbx_ip){s.select,0,:}))) {
        if (dns_query($(avp(domain_pbx_ip){s.select,0,:}), "xyz")) {
            $var(i) = 0;
            while ($var(i) < $dns(xyz=>count)) {
                $sht(pass_thru_auth=>$dns(xyz=>addr[$var(i)])) = $var(rd_orig);
                $var(i) = $var(i) + 1;
            }
        }
    } else {
        $sht(pass_thru_auth=>$(avp(domain_pbx_ip){s.select,0,:})) = $var(rd_orig);
    }

#!ifdef WITH_PUSH
    # ── Contact substitution for FusionPBX ──
    # Replace iOS device Contact (dynamic NAT IP) with stable SBC address.
    # FusionPBX stores this Contact and forwards inbound calls back to dSIPRouter.
    # Expires=86400 (24h) so FusionPBX retains the route even while iPhone is suspended.
    if ($hdr(Expires) != "0") {
        remove_hf("Contact");
        append_hf("Contact: <sip:$tU@sbc.myline.tel:5060>;expires=86400\r\n");
        remove_hf("Expires");
        append_hf("Expires: 86400\r\n");
        xlog("L_INFO", "[REGISTER] forwarding stable contact for $tU (Expires=86400)\n");
    } else {
        # Unregister: MUST use the SAME sbc.myline.tel contact that FusionPBX
        # has stored, otherwise FusionPBX won't recognize it and silently ignores.
        remove_hf("Contact");
        append_hf("Contact: <sip:$tU@sbc.myline.tel:5060>;expires=0\r\n");
        xlog("L_INFO", "[REGISTER] forwarding unregister for $tU\n");
    }
#!endif

    route(RELAY);
    exit;
}
```

### 7.3 — Relax `sanity_check` for iOS UDP clients

In `route[REQINIT]` (around line 1820):

```cfg
# 1479 = 1511 - 32 (removed Content-Length check). Content-Length is
# OPTIONAL on UDP SIP per RFC 3261 and the iPhone's SIP library does
# not always include it — without this fix every iPhone outbound INVITE
# is silently dropped at sanity_check() before reaching the dialplan.
if (!sanity_check("1479", "7")) {
    xlog("L_WARN", "Malformed SIP message from source address $si:$sp\n");
    exit;
}
```

Also keep the special-case OPTIONS handlers BEFORE the sanity check:

```cfg
# iOS push clients use OPTIONS every 15s for NAT keepalive.
# They are not in the trusted address list but ARE registered.
if (is_method("OPTIONS") && registered("location")) {
    sl_send_reply("200", "Keepalive");
    exit;
}
```

### 7.4 — Reduce natping interval for fast NAT recovery

In nathelper modparams (around line 730):

```cfg
# natping_interval=20: send OPTIONS to NAT'd clients every 20s.
# Most consumer/cellular NAT expires UDP bindings at 30s of silence.
# 20s gives margin and keeps the iPhone reachable even if the app
# stops sending its own keepalive (e.g. briefly backgrounded).
modparam("nathelper", "natping_interval", 20)
modparam("nathelper", "ping_nated_only", 1)
modparam("nathelper", "sipping_bflag", FLB_NATSIPPING)
modparam("nathelper", "sipping_from", "sip:pinger@UAC_REG_ADDR")
```

### 7.5 — In-dialog BYE relay (fix for iOS hangup)

In `route[WITHINDLG]` (around line 1980), AFTER the `loose_route_mode("1")` branch:

```cfg
if (is_method("ACK|UPDATE|INVITE|BYE|PRACK")) {
    route(DLGURI);

    if (is_method("BYE")) {
        setflag(FLT_ACC);
        setflag(FLT_ACCFAILED);
        route(RTPENGINEDELETE);
    }

    if (t_check_trans()) {
        route(RELAY);
        exit;
    }

    # In-dialog request (has To-tag) but no matching transaction.
    # BYE is always a new transaction within the dialog. Relay it
    # unconditionally so iPhone-originated BYEs reach the far side
    # instead of being dropped with 481 ("Call/Transaction Does Not Exist").
    # Without this, "hang up from iPhone" leaves the other party connected.
    if (has_totag() && is_method("BYE|UPDATE|INVITE|PRACK")) {
        xlog("L_INFO", "[WITHINDLG] in-dialog $rm without route, relaying to $ru\n");
        route(RELAY);
        exit;
    }

    sl_send_reply("481", "Call/Transaction Does Not Exist");
    exit;
}
```

### 7.6 — Inbound INVITE handling (push trigger + URI cleanup)

In `route[LOCATION]`, after the `lookup()` failure path that goes to push:

```cfg
# ── Recover the real registrar domain ──
# FusionPBX puts our SBC domain (sbc.myline.tel) in the INVITE To-header,
# but the iPhone registered as USER@mltpbx.myline.tel. Use the pass_thru_auth
# htable (populated during REGISTER) to recover the real domain for lookup.
$var(reg_domain) = $sht(pass_thru_auth=>$si);
if (!strempty($var(reg_domain)) && $rd != $var(reg_domain)) {
    xlog("L_INFO", "[LOCATION] pass_thru_auth: rewriting $rd=$rd -> $var(reg_domain) for lookup\n");
    $rd = $var(reg_domain);
}

if (!lookup("location", "sip:$rU@$rd")) {
    # No registration found → push the device (route[SENDPUSH] + route[SUSPEND])
    route(SENDPUSH);
    route(SUSPEND);
    exit;
}

# ── Push-capable device handling ──
# If the resolved Contact has pn-prid, this is an iOS push client.
# Set a short failure timer so we send a push if the device is silent.
# Strip the pn-* URI params before forwarding — the iPhone's SIP library
# silently drops INVITEs whose R-URI contains non-standard URI params
# (no 100 Trying, no 180 Ringing).
#!ifdef WITH_PUSH
if (is_method("INVITE") && ($ru =~ "pn-prid=")) {
    xlog("L_INFO", "[LOCATION] push-capable device, short timer for $rU@$rd\n");
    t_set_fr(30000, 5000);
    t_on_failure("PUSH_FAIL");

    # Strip pn-provider, pn-param, pn-prid from R-URI
    $ru = $(ru{re.subst,/;pn-provider=[^;>]*//});
    $ru = $(ru{re.subst,/;pn-param=[^;>]*//});
    $ru = $(ru{re.subst,/;pn-prid=[^;>]*//});
    xlog("L_INFO", "[LOCATION] stripped pn-* params, ru=$ru\n");
}
#!endif
```

### 7.7 — Push routes (`SENDPUSH`, `SUSPEND`, `PUSH_FAIL`, `RESUME`, `JOIN`)

These routes implement the suspend-and-push-resume pattern. Add them as new routes in kamailio.cfg.

```cfg
route[SENDPUSH] {
    # Use pass_thru_auth[$si] to get the registrar domain (the iPhone's
    # actual registered domain), regardless of what's in the INVITE.
    $var(reg_domain) = $sht(pass_thru_auth=>$si);
    if (!strempty($var(reg_domain))) {
        $var(push_aor) = $tU + "@" + $var(reg_domain);
    } else {
        $var(push_aor) = $tU + "@" + $td;
    }

    xlog("L_INFO", "[SENDPUSH] Checking push token for $var(push_aor) (si=$si reg_domain=$var(reg_domain))\n");
    $var(pn_token) = $sht(pushtok=>$var(push_aor));
    if ($var(pn_token) == $null) {
        xlog("L_INFO", "[SENDPUSH] No token for $var(push_aor) - not a push client\n");
        return;
    }

    xlog("L_INFO", "[SENDPUSH] APNs push to $tU@$var(reg_domain) token=$(var(pn_token){s.substr,0,8})... caller=$fU\n");

    # POST to local PHP bridge which signs APNs JWT and pushes via HTTP/2.
    # PUSH_SECRET must match voip_push.php
    $var(http_body) = '{"secret":"PUSH_SECRET","token":"' + $var(pn_token) + '","caller":"' + $fU + '","caller_name":"' + $fn + '"}';
    http_async_query("http://127.0.0.1:8070/push", "PUSH_CB");
}

# Async callback — we don't actually care about the response
route[PUSH_CB] {
    xlog("L_INFO", "[PUSH_CB] APNs response: ok=$http_ok body=$http_rb\n");
}

route[SUSPEND] {
    xlog("L_INFO", "suspended transaction [$T(id_index):$T(id_label)] $fU => $tU@$td\n");

    # Store join key under the REGISTRAR domain so route[JOIN] (triggered
    # by REGISTER) can find this suspended INVITE.
    $var(join_reg_domain) = $sht(pass_thru_auth=>$si);
    if (!strempty($var(join_reg_domain))) {
        $var(join_aor) = $tU + "@" + $var(join_reg_domain);
    } else {
        $var(join_aor) = $tU + "@" + $td;
    }
    $sht(push=>join::$var(join_aor)) = "" + $T(id_index) + ":" + $T(id_label);

    t_suspend();
}

failure_route[PUSH_FAIL] {
    # INVITE reached the registered device but no response within timer.
    # Device is probably suspended — send push and re-suspend.
    if (t_check_status("408|480|503") || !t_any_replied()) {
        $var(pf_reg_domain) = $sht(pass_thru_auth=>$si);
        if (!strempty($var(pf_reg_domain))) {
            $var(pf_join_aor) = $tU + "@" + $var(pf_reg_domain);
        } else {
            $var(pf_join_aor) = $tU + "@" + $td;
        }
        $sht(push=>join::$var(pf_join_aor)) = "" + $T(id_index) + ":" + $T(id_label);
        xlog("L_INFO", "[PUSH_FAIL] join key set for $var(pf_join_aor)\n");
        route(SENDPUSH);
        exit;
    }
}

route[RESUME] {
    xlog("L_INFO", "resuming transaction\n");

    # Use pass_thru_auth[$si] to find the actual registered Contact.
    $var(resume_domain) = $sht(pass_thru_auth=>$si);
    if (!strempty($var(resume_domain))) {
        $var(resume_aor) = $rU + "@" + $var(resume_domain);
    } else {
        $var(resume_aor) = $rU + "@" + $rd;
    }
    xlog("L_INFO", "values before lookup: rm=$rm ru=$rU rd=$rd du=$du resume_aor=$var(resume_aor)\n");

    if (!lookup("location", "sip:$var(resume_aor)")) {
        sl_send_reply("404", "Not Found");
        exit;
    }

    record_route();
    t_relay();
    exit;
}

route[JOIN] {
    xlog("L_INFO", "[JOIN] resuming suspended transaction for $tU@$td\n");
    if ($sht(push=>join::$tU@$td) != $null) {
        # Parse "id_index:id_label" from htable
        $var(tids) = $sht(push=>join::$tU@$td);
        $var(idx) = $(var(tids){s.select,0,:});
        $var(lbl) = $(var(tids){s.select,1,:});
        if (t_continue("$var(idx)", "$var(lbl)", "RESUME")) {
            $sht(push=>join::$tU@$td) = $null;
        }
    }
}
```

Hook `route(JOIN)` into the REGISTER handler so a fresh REGISTER resumes any waiting INVITE:

```cfg
# At the end of REGISTRAR handling (after route(RELAY)):
#!ifdef WITH_PUSH
if ($sht(push=>join::$tU@$td) != $null) {
    xlog("L_INFO", "[REGISTER] [PUSH] about to un-suspend transaction for $tU@$td\n");
    route(JOIN);
}
#!endif
```

### 7.8 — WhatsApp voice routing in RTPENGINEOFFER

In `route[RTPENGINEOFFER]`, BEFORE the default branch:

```cfg
# - WhatsApp call FROM Meta (inbound voice): they use unencrypted RTP/AVP
#   on the encrypted SIP/TLS leg. Transcode to PCMU/PCMA which Meta requires.
else if ($fu =~ "wa.meta.vc") {
    $var(reflags) = "trust-address replace-origin replace-session-connection rtcp-mux-demux ICE=remove transcode-PCMU transcode-PCMA RTP/AVP";
}
```

### 7.9 — WhatsApp voice routing in RTPENGINEANSWER

In `route[RTPENGINEANSWER]`, BEFORE the default branch:

```cfg
# - WhatsApp call TO Meta (outbound voice): Meta expects SRTP (RTP/SAVP).
#   Transcode to PCMU/PCMA which Meta accepts.
else if ($fu =~ "wa.meta.vc") {
    $var(reflags) = "trust-address replace-origin replace-session-connection rtcp-mux-demux ICE=remove transcode-PCMU transcode-PCMA RTP/SAVP";
}
```

### 7.10 — WhatsApp request URI rewriting + tagging

For outbound INVITE going to Meta, strip the leading `+` from the called number (Meta doesn't expect it):

```cfg
# In the outbound routing route (after gateway selection):
if ($fu =~ "wa.meta.vc" && $rU =~ "^\+") {
    $rU = $(rU{s.substr,1,0});
    xlog("L_WARN", "DEBUG STRIP: rU after strip=$rU\n");
}
```

For inbound INVITE from Meta, tag the From-host so downstream routes know it's a WhatsApp call:

```cfg
# Add X-WhatsApp-Call header before relaying to FusionPBX
if ($fu =~ "wa.meta.vc") {
    append_hf("X-WhatsApp-Call: true\r\n");
}
```

And set a branch flag for source classification:

```cfg
# In route[REQINIT] source classification block:
else if (allow_source_address(FLT_CARRIER)) {
    setbflag(FLB_SRC_CARRIER);
    if ($fu =~ "wa.meta.vc") { setbflag(FLB_SRC_WHATSAPP); }
}
```

### 7.11 — Meta auth injection in failure_route

In `failure_route[MANAGE_FAILURE]`, BEFORE the existing `if (t_check_status("401|407") && !strempty($avp(auth_user)))` block:

```cfg
# ── Dynamic WhatsApp/Meta auth injection ──
# When Meta responds 401/407 to an outbound INVITE, look up the credentials
# from uacreg (rows with flags=13) and inject them into $avp(auth_user/pass/realm)
# so the next branch (uac_auth + t_relay) can satisfy the challenge.
if (t_check_status("401|407") && $dlg_var(dst_gwgroupid) == "13" && strempty($avp(auth_user))) {
    if (sql_query("kam", "SELECT auth_username, auth_password, realm FROM uacreg WHERE flags=13 LIMIT 1", "ra") && $dbr(ra=>rows) > 0) {
        $avp(auth_user) = $dbr(ra=>[0,0]);
        $avp(auth_pass) = $dbr(ra=>[0,1]);
        $avp(auth_realm) = $dbr(ra=>[0,2]);
        xlog("L_WARN", "DEBUG: Injected Meta auth: user=$avp(auth_user)\n");
    }
}

# Existing uac_auth block — handles the actual digest response
if (t_check_status("401|407") && !strempty($avp(auth_user))) {
    t_drop_replies();
#!ifdef WITH_UAC
    if (!(uac_auth() && t_relay())) {
        xlog("L_INFO", "UAC Authentication failed\n");
        t_reply("503","Service not available");
        route(RTPENGINEDELETE);
    }
    exit;
#!endif
}
```

> If you need to support more Meta phone numbers, just add more `uacreg` rows with `flags=13`. The `LIMIT 1` in the query returns the first matching row — this is fine for single-number Meta integration. For per-domain Meta accounts, change `flags=13` to `flags=<domain_specific_value>` and select by the originating domain.

### 7.12 — Validate and restart

```bash
kamailio -c /etc/kamailio/kamailio.cfg 2>&1 | grep -iE 'error|syntax'
# If clean:
systemctl restart kamailio
systemctl status kamailio
```

---

## Part 8 — APNs iOS push setup

### Step 8.1 — Generate APNs Auth Key

1. https://developer.apple.com/account/resources/authkeys/list
2. **+** → **Apple Push Notifications service (APNs)**
3. Download the `.p8` file (one-time download — save it)
4. Note the **Key ID** (10 chars, e.g. `ABCD1234XY`)
5. Note your **Team ID** (top-right of dev portal)

### Step 8.2 — Deploy the push files on dSIPRouter

```bash
mkdir -p /etc/myline
cp AuthKey_ABCD1234XY.p8 /etc/myline/
chown www-data:www-data /etc/myline/AuthKey_ABCD1234XY.p8
chmod 600 /etc/myline/AuthKey_ABCD1234XY.p8
```

Deploy the PHP bridge (the file `voip_push_bridge.php` in this repo):

```bash
cp voip_push_bridge.php /var/www/html/voip_push.php
chown www-data:www-data /var/www/html/voip_push.php
chmod 644 /var/www/html/voip_push.php
```

Edit the file and set:
- `APNS_KEY_FILE` → path to the `.p8`
- `APNS_KEY_ID` → from step 8.1
- `APNS_TEAM_ID` → from step 8.1
- `APNS_BUNDLE_ID` → `com.mylinetelecom.softphone`
- `PUSH_SECRET` → strong random (must match the one in kamailio's `route[SENDPUSH]`)
- `APNS_PRODUCTION` → `true` for App Store builds, `false` for sandbox

### Step 8.3 — Test push directly

```bash
curl -sX POST http://127.0.0.1/voip_push.php \
  -H 'Content-Type: application/json' \
  -d '{
    "secret":"YOUR_PUSH_SECRET",
    "token":"<60-char-hex-from-dsip_push_tokens>",
    "caller":"+13055551234",
    "caller_name":"Test"
  }'
```

Expected: `{"status":"sent","token":"abc12345..."}` and the iPhone shows an incoming-call screen within ~1 second.

### Step 8.4 — Verify the kamailio→PHP wire

After receiving an INVITE for a suspended user, kamailio's `route[SENDPUSH]` POSTs to `http://127.0.0.1:8070/push` (or whatever URL you used). If you're using `voip_push.php` at port 80, change the URL in `route[SENDPUSH]` to match:

```cfg
http_async_query("http://127.0.0.1/voip_push.php", "PUSH_CB");
```

---

## Part 9 — FusionPBX trusted-SBC ACL

The single most important FusionPBX change for SBC integration. Without it, all calls from the SBC land in the `public` context and fail with 480.

### Step 9.1 — Create the `trusted_sbc` ACL

**FusionPBX UI → Advanced → Access Controls → +Add:**

| Field | Value |
|-------|-------|
| Name | `trusted_sbc` |
| Default | `deny` |
| Description | `Outbound proxy / SBC servers` |

**Then click Edit → Nodes → +Add:**

| Field | Value |
|-------|-------|
| Type | `cidr` |
| Allow | `allow` |
| CIDR | `149.28.109.210/32` (your SBC IP) |
| Description | `dSIPRouter SBC` |

### Step 9.2 — Remove the SBC IP from `providers` ACL

In the same Access Controls page, find the **`providers`** node, edit it, and remove `149.28.109.210` if present. Carrier IPs should remain in `providers`; the SBC should NOT (this caused outbound-from-iPhone to 480 in our debugging).

### Step 9.3 — Attach the new ACL to the internal SIP profile

**FusionPBX UI → Advanced → SIP Profiles → `internal` → settings:**

Find `apply-inbound-acl`. If present, edit its value to `trusted_sbc` (comma-separate if other ACLs already there). If missing, +Add it:

| Parameter | Value |
|-----------|-------|
| `apply-inbound-acl` | `trusted_sbc` |

If you cannot find the setting via UI, edit the XML directly:

```bash
nano /etc/freeswitch/sip_profiles/internal.xml
# Inside <profile><settings>:
#   <param name="apply-inbound-acl" value="trusted_sbc"/>
```

### Step 9.4 — Reload

```bash
fs_cli -x "reloadacl"
fs_cli -x "sofia profile internal restart"
```

### Step 9.5 — Verify

```bash
fs_cli -x "show acl trusted_sbc"
# Should show: 149.28.109.210/32 allow
```

After a test outbound call from any tenant's softphone, the CDR should show:

```
sip_acl_authed_by:  trusted_sbc        ← was: providers
direction:          outbound           ← was: inbound
context:            <customer-domain>  ← was: public
```

---

## Part 10 — Per-tenant checklist

For every new customer domain added to FusionPBX:

| Step | Where | Action |
|------|-------|--------|
| 10.1 | FusionPBX UI → Accounts → Domains | Add the customer domain (e.g. `acme.myline.tel`) |
| 10.2 | FusionPBX UI → Accounts → Extensions | Create each extension. **Set Outbound Caller ID Number = the customer's DID (full E.164, e.g. `13055551234`)** |
| 10.3 | FusionPBX UI → Dialplan → Destinations | For each DID, assign **Provider** (SMS provider — e.g. Thinq) |
| 10.4 | FusionPBX UI → Dialplan → Outbound Routes | Add an outbound rule that routes to ASTPP (or your carrier) |
| 10.5 | dSIPRouter UI → Domain Routing → +Add Domain | Add the domain with PBX Cluster IP pointing to FusionPBX |
| 10.6 | (Optional) Meta phone | Verify the DID with Meta if you want WhatsApp on this number |
| 10.7 | (Optional) dSIPRouter `uacreg` | If Meta-registered, add a row with `flags=13`, realm=`wa.meta.vc` |
| 10.8 | (Optional) FusionPBX → WhatsApp module | Configure per-tenant Meta credentials if using per-tenant tokens |

### SQL to verify a tenant is set up correctly

```bash
psql -U fusionpbx -d fusionpbx <<SQL
\set domain 'acme.myline.tel'
SELECT
  d.domain_name,
  e.extension,
  e.outbound_caller_id_number,
  (SELECT COUNT(*) FROM v_destinations
    WHERE domain_uuid = d.domain_uuid
      AND destination_enabled='true'
      AND provider_uuid IS NOT NULL) AS dids_with_provider
FROM v_domains d
JOIN v_extensions e ON e.domain_uuid = d.domain_uuid
WHERE d.domain_name = :'domain'
ORDER BY e.extension;
SQL
```

Every extension should have `outbound_caller_id_number` set. The `dids_with_provider` count should be ≥ 1.

---

## Part 11 — Verification

After completing the deployment, run these tests in order. Each tests one slice of the stack.

### 11.1 — REGISTER + push token storage

From iPhone, register. Then:

```bash
mysql -u root kamailio -e "SELECT account, LEFT(push_token,16) AS token, updated_at FROM dsip_push_tokens;"
```

Expected: one row per registered iPhone with a non-empty push_token.

### 11.2 — Outbound SMS

Send SMS from softphone to a 10-digit number. Watch:

```bash
tail -f /tmp/sms_outbound.log
```

Expected: `[SMS] Sent via queue <uuid> from <DID> to <number>`.

### 11.3 — Outbound WhatsApp

Send a WhatsApp message from softphone (11-digit number with leading 1):

```bash
tail -f /tmp/sms_outbound.log
```

Expected: `[SMS] WhatsApp free-form from <DID> to <number>`.

### 11.4 — Inbound voice call (app foreground)

Have someone call extension 231 with the app open. Watch dSIPRouter:

```bash
journalctl -u kamailio --since "1 minute ago" | grep -E "INVITE|LOCATION|180|200"
```

Expected: INVITE → LOCATION → relay to iPhone → 180 Ringing → 200 OK.

### 11.5 — Inbound voice call (app suspended → push wake)

Force-quit the app, wait 30 seconds, have someone call. Watch:

```bash
journalctl -u kamailio --since "1 minute ago" | grep -iE "SENDPUSH|SUSPEND|JOIN|RESUME|push.*token"
```

Expected: `[SENDPUSH] APNs push to … token=…` followed by `[REGISTER] [PUSH] about to un-suspend` and `[RESUME]`.

### 11.6 — Outbound voice call

From the registered iPhone, dial a number. Watch:

```bash
journalctl -u kamailio --since "1 minute ago" | grep -E "INVITE|RELAY|200|480"
```

Expected: INVITE → RELAY to FusionPBX → 200 OK. **No 480.**

In FusionPBX → Reports → CDR, the resulting CDR should show:
- `direction: outbound`
- `context: <customer-domain>` (NOT `public`)
- `sip_acl_authed_by: trusted_sbc`

### 11.7 — WhatsApp voice (inbound from Meta)

Have someone call your WhatsApp Business number. Watch:

```bash
journalctl -u kamailio --since "1 minute ago" | grep -iE "wa.meta.vc|WhatsApp|meta"
```

Expected: INVITE from `wa.meta.vc` → matches `dr_rules` → relayed to FusionPBX with `X-WhatsApp-Call: true`.

### 11.8 — WhatsApp voice (outbound to Meta)

Initiate a call from softphone to a Meta number. Watch:

```bash
journalctl -u kamailio --since "1 minute ago" | grep -iE "wa.meta.vc|MANAGE_FAILURE|Injected Meta"
```

Expected: INVITE → routed to gwgroup 13 → 401 from Meta → `Injected Meta auth: user=<DID>` → 200 OK.

---

## Part 12 — Troubleshooting

### SMS

| Symptom | Cause | Fix |
|---------|-------|-----|
| FreeSWITCH log: `index.lua: No such file or directory` | Scripts not deployed | Re-run Part 3.2–3.4 |
| `Domain not found: <host>` | FusionPBX domain doesn't match the From-host | Compare `SELECT domain_name FROM v_domains` to SIP REGISTER From-header |
| `No provider for DID` | Extension's Outbound Caller ID Number missing | Part 10.2 |
| `No provider found for domain` | No destination has a provider assigned | Part 10.3 |
| Queued but undelivered | Thinq credentials wrong | FusionPBX → Messages → Providers |
| `Call to undefined method permissions::new()` | Older FusionPBX | `sed -i 's/permissions::new()/new permissions()/g' /usr/share/freeswitch/scripts/app/sms/send.php` |
| iOS shows 5 copies of incoming message | iOS app race condition (fixed in app: `ChatMessageDao.insertIfNotDuplicate`) | Build current iOS version |

### WhatsApp text

| Symptom | Cause | Fix |
|---------|-------|-----|
| Free-form rejected with Meta error 131047 | Outside 24h conversation window | Send a template first |
| All outbound fails | Meta token expired | Generate new system-user token (Part 5.3) |
| Inbound webhooks not arriving | Webhook URL not reachable or verify token mismatch | Re-check Part 4.4 |

### WhatsApp voice

| Symptom | Cause | Fix |
|---------|-------|-----|
| Outbound returns 401 from Meta repeatedly | uacreg row missing or wrong credentials | Verify `SELECT * FROM uacreg WHERE flags=13;` |
| `DEBUG: Injected Meta auth: user=<NULL>` | `flags=13` row doesn't exist | Add row per Part 6.5 |
| One-way audio | SRTP/RTP mismatch | Verify Part 7.8 / 7.9 have RTP/SAVP on answer and RTP/AVP on offer |
| Inbound from Meta rejected | Source IP not whitelisted | Add IP to `address` grp=8 (Part 6.4), `kamcmd permissions.addressReload` |

### iOS Push

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Cannot read APNs key file` | `.p8` file path wrong or not readable | Check `APNS_KEY_FILE` in `voip_push.php` |
| APNs returns 400 `BadDeviceToken` | Token is for sandbox but `APNS_PRODUCTION=true` (or vice versa) | Match environment to app build |
| APNs returns 403 `InvalidProviderToken` | Key ID / Team ID / Bundle ID wrong | Verify all three |
| Push delivered but iOS doesn't ring | iOS app PushKit handler bug or stale `pushPrereportedUUID` | Build current iOS (bug B fixed) |
| `pushtok` htable empty after REGISTER | Contact missing pn-prid OR regex wrong | Verify iOS app appends `;pn-prid=` to Contact and Part 7.2 regex matches |

### SBC ACL / outbound

| Symptom | Cause | Fix |
|---------|-------|-----|
| Outbound INVITE returns 480 from FusionPBX, CDR shows `context: public` | SBC IP in `providers` ACL | Part 9.1–9.2 |
| `show acl trusted_sbc` empty after edit | ACL not reloaded | `fs_cli -x "reloadacl"` |
| `Discarding stale pushPrereportedUUID` log line on every 2nd call | iOS app side (fixed in current build) | Rebuild iOS |

### Hangup

| Symptom | Cause | Fix |
|---------|-------|-----|
| iPhone hangup leaves other party connected | iPhone library omits Route header; dSIPRouter returned 481 | Part 7.5 (in-dialog BYE relay) |
| Other party hangs up but iPhone shows "in call" | iPhone library only echoes ONE Via in BYE 200 OK + Call-ID didn't match | iOS app fix (bug C: dialog-tag fallback in BYE dispatcher) |

---

## Part 13 — File inventory

Repo source → server target mapping.

| Source (repo) | Target (server) | Owner | Mode | Component |
|---------------|-----------------|-------|------|-----------|
| `sms_send.php` | `/usr/share/freeswitch/scripts/app/sms/send.php` (FusionPBX) | www-data | 644 | SMS+WhatsApp text outbound bridge |
| (inline Part 3.2) | `/usr/share/freeswitch/scripts/app/sms/index.lua` (FusionPBX) | www-data | 644 | SMS chatplan dispatcher |
| `voip_push_bridge.php` | `/var/www/html/voip_push.php` (dSIPRouter) | www-data | 644 | APNs sender |
| `freeswitch_push.lua` | `/usr/share/freeswitch/scripts/voip_push.lua` (FusionPBX, only without SBC) | www-data | 644 | Push trigger for direct-FusionPBX deployments |
| Your APNs `.p8` key | `/etc/myline/AuthKey_<KEYID>.p8` (dSIPRouter) | www-data | 600 | APNs signing key |
| `<your private app/whatsapp/ source>` | `/var/www/fusionpbx/app/whatsapp/` (FusionPBX) | www-data | 644 dirs 755 | WhatsApp text module |

Files modified (existed already, you edit them):

| Target | What changes |
|--------|-------------|
| FusionPBX `/etc/freeswitch/sip_profiles/internal.xml` | Add `apply-inbound-acl=trusted_sbc` |
| FusionPBX `/etc/freeswitch/chatplan/default.xml` | Verify dispatcher to `app/sms/index.lua` exists |
| dSIPRouter `/etc/kamailio/kamailio.cfg` | All Part 7 changes |
| FusionPBX `v_extensions.outbound_caller_id_number` | Per-extension DID |
| FusionPBX `v_destinations.provider_uuid` | Per-DID provider |
| dSIPRouter `domain`, `dr_gateways`, `dr_gw_lists`, `dr_rules`, `address`, `uacreg` | Per Part 6 |
| FusionPBX Access Controls (in DB: `v_access_controls`, `v_access_control_nodes`) | `trusted_sbc` ACL with SBC IP node |

---

## Part 14 — Upgrade notes

### Before upgrading FusionPBX

```bash
pg_dump -U fusionpbx fusionpbx > /root/fusionpbx-pre-upgrade-$(date +%F).sql
cp -r /var/www/fusionpbx /root/fusionpbx-pre-upgrade-$(date +%F).code
```

Things FusionPBX upgrades have historically broken:

- `v_extensions.user_uuid` removed → moved to junction table `v_extension_users`. The current `sms_send.php` already handles this case (does NOT join — sets `user_uuid=null` for queue insert).
- `v_destinations.provider_uuid` renamed → re-check column existence after upgrade.
- `apply-inbound-acl` in `internal.xml` reset to default. Re-apply Part 9.3.

### Before upgrading dSIPRouter

```bash
mysqldump -u root kamailio > /root/kamailio-pre-upgrade-$(date +%F).sql
cp /etc/kamailio/kamailio.cfg /root/kamailio.cfg.pre-upgrade-$(date +%F)
```

dSIPRouter UI upgrades regenerate `kamailio.cfg` from a template. **All Part 7 changes WILL be overwritten.** Strategy:

1. Diff your current `kamailio.cfg` against the template that comes with the new dSIPRouter version
2. Cherry-pick each Part 7 patch back into the new file
3. Restart kamailio
4. Re-run verification (Part 11)

The database tables (`domain`, `dr_gateways`, `uacreg`, `dsip_push_tokens`, etc.) are usually preserved across dSIPRouter upgrades — but verify with `SHOW TABLES;` before and after.

### After any upgrade

Run the **full Part 11 verification suite** end-to-end. Take ~20 minutes — it covers every path that could break.

---

## Appendix A — Multi-tenant settings matrix

| Setting | Per-tenant? | Notes |
|---------|-------------|-------|
| Extension Outbound Caller ID Number | Yes | Each extension in each domain |
| DID Provider assignment | Yes | DID belongs to a single domain |
| Outbound dialplan to ASTPP | Yes | Each customer can have own ASTPP creds |
| Meta phone number (for WhatsApp text + voice) | Yes (one Meta number per DID) | Verify with Meta in Part 5.2 |
| Meta system-user token | Optional per-tenant (default: global) | Per-tenant if customers want separate billing/templates |
| `uacreg` row | Yes (one per Meta-registered DID) | Distinct `l_username` |
| `trusted_sbc` ACL on FusionPBX | No (global) | One SBC serves all tenants |
| `/usr/share/freeswitch/scripts/app/sms/*` | No (global) | Same scripts handle all domains |
| `voip_push.php` + APNs key | No (global) | Push token per-device, key per-app-bundle |

---

## Appendix B — Ports and addresses reference

| Endpoint | Port | Protocol | Used for |
|----------|------|----------|----------|
| dSIPRouter | 5060 | UDP | SIP from softphones / SIP to FusionPBX |
| dSIPRouter | 5061 | TLS | SIP to Meta (`wa.meta.vc`) |
| dSIPRouter | 5000 | HTTPS | Admin UI |
| FusionPBX `internal` | 5060 | UDP | SIP from SBC / direct softphones |
| FusionPBX `external` | 5080 | UDP | SIP from carriers (Twilio etc.) |
| FusionPBX | 443 | HTTPS | Admin UI + WhatsApp webhook |
| iOS softphone | 5090 | UDP | SIP listener (registration source port) |
| APNs prod | 443 | HTTPS/2 | `api.push.apple.com` |
| APNs sandbox | 443 | HTTPS/2 | `api.sandbox.push.apple.com` |
| Meta Graph API | 443 | HTTPS | `graph.facebook.com` |
| Meta SIP | 5061 | TLS | `wa.meta.vc` |

---

## Appendix C — Database table cheat sheet (dSIPRouter)

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `domain` | Hosted SIP domains (multi-tenant) | `id, domain, did` |
| `address` | Trusted source IPs grouped by `grp` (8=carriers/meta-whitelist) | `id, grp, ip_addr, mask, port, tag` |
| `dr_gateways` | Gateway endpoints (FusionPBX, Meta, carriers) | `gwid, address, attrs, description` |
| `dr_gw_lists` | Groups of gateways with load-balancing | `id, gwlist, description` |
| `dr_rules` | Routing rules: prefix → gw_list | `ruleid, groupid, prefix, gwlist, description` |
| `uacreg` | UAC credentials (Meta auth via `flags=13`) | `id, l_username, r_username, realm, auth_username, auth_password, flags` |
| `dsip_push_tokens` | iOS APNs tokens (DB-backed pushtok htable) | `account, push_token, updated_at` |
| `location` | usrloc registrations (regular SIP usrloc table) | `username, domain, contact, expires, received` |
| `dsip_maintmode` | Maintenance mode flags per gateway | `ipaddr, gwid` |
| `dsip_lcr` | Least-cost routing patterns | `pattern, dr_groupid` |
| `dsip_call_settings_h` | Per-gateway call limits | `gwgroupid, limit, timeout` |

---

*End of guide. Maintained alongside the My Line Telecom softphone codebase. Last updated: 2026-05-15.*

*For questions about this deployment, see the codebase at `C:\Claude_Code\` — the canonical source for all referenced PHP/Lua files.*
