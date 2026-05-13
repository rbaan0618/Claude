<?php
// Bridge SIP MESSAGE from FreeSWITCH to FusionPBX message queue
if (!isset($argv[1])) { error_log("[SMS] No arguments"); exit(1); }

parse_str($argv[1], $args);
$from_ext  = preg_replace('/[^0-9]/', '', $args['from']   ?? '');
$from_host = trim($args['domain'] ?? '');
$to_raw    = trim($args['to']     ?? '');
// X-Channel header value forwarded by FreeSWITCH (may be absent — default to '').
// We intentionally do NOT default to 'sms' here; when the header is missing we
// rely on the digit count of the TO number to determine the delivery channel.
$channel   = strtolower(trim($args['channel'] ?? ''));
$body      = trim($args['body']   ?? '');
$template_name = trim($args['template'] ?? '');
$template_lang = trim($args['lang']     ?? 'en_US');

// Thinq routing via digit count — the softphone encodes intent in number format:
//   10 digits (no country code) → SMS (Thinq forces SMS for 10-digit DID)
//   11 digits with leading 1    → omnichannel (Thinq delivers WhatsApp if registered,
//                                  falls back to SMS otherwise)
//
// When the caller explicitly sets channel=sms AND sends an 11-digit US number,
// we strip the leading 1 to force SMS.  This handles edge cases where a user
// intentionally sends SMS to a number that could otherwise receive WhatsApp.
// In all other cases (channel=whatsapp, channel absent, or 10-digit) we leave
// the digit string untouched and let Thinq's omnichannel logic decide.
$to_digits = preg_replace('/[^0-9]/', '', $to_raw);
if ($channel === 'sms' && strlen($to_digits) === 11 && $to_digits[0] === '1') {
    $to = substr($to_digits, 1);   // Force SMS: strip leading 1 → 10 digits
} else {
    $to = $to_digits;              // WhatsApp / auto: keep 11 digits for omnichannel
}
error_log("[SMS] channel='" . ($channel ?: 'auto') . "' to_raw=$to_raw to_digits=$to_digits to=$to template=" . ($template_name ?: 'none'));

if (!$from_ext || !$to || !$from_host) {
    error_log("[SMS] Missing params: from=$from_ext to=$to domain=$from_host");
    exit(1);
}
if (!$body && !$template_name) {
    error_log("[SMS] No body and no template");
    exit(1);
}

// Bootstrap FusionPBX (needed for uuid(), permissions, and database->save())
$conf = glob("{/usr/local/etc,/etc}/fusionpbx/config.conf", GLOB_BRACE);
if (empty($conf)) { error_log("[SMS] FusionPBX config not found"); exit(1); }
$ini = parse_ini_file($conf[0]);
set_include_path($ini['document.root'] ?? '/var/www/fusionpbx');
require_once "resources/require.php";

// ── Direct PDO connection ─────────────────────────────────────────────────────
// The FusionPBX database class can be unreliable for SELECT queries in CLI
// context (no session, no domain context).  We use PDO directly for all reads
// so that extension DID lookup works regardless of FusionPBX state.
// The FusionPBX database->save() is kept only for queue insertion (proven to
// work for regular sends).
//
// FusionPBX config.conf key names vary by version — try both formats.
$db_host = $ini['database.0.host'] ?? $ini['database.host'] ?? '127.0.0.1';
$db_port = $ini['database.0.port'] ?? $ini['database.port'] ?? '5432';
$db_name = $ini['database.0.name'] ?? $ini['database.name'] ?? 'fusionpbx';
$db_user = $ini['database.0.username'] ?? $ini['database.username'] ?? 'fusionpbx';
$db_pass = $ini['database.0.password'] ?? $ini['database.password'] ?? '';

try {
    $pdo = new PDO(
        "pgsql:host=$db_host;port=$db_port;dbname=$db_name",
        $db_user, $db_pass,
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );
} catch (Exception $e) {
    error_log("[SMS] PDO connect failed: " . $e->getMessage());
    exit(1);
}

// Get domain_uuid from domain name
$stmt = $pdo->prepare("SELECT domain_uuid::text FROM v_domains WHERE domain_name = ? LIMIT 1");
$stmt->execute([$from_host]);
$domain_uuid = $stmt->fetchColumn();
if (!$domain_uuid) { error_log("[SMS] Domain not found: $from_host"); exit(1); }

// Get outbound_caller_id_number for this extension (the DID to send from).
// user_uuid lives in v_extension_users (junction table) — not needed for
// delivery, so we skip the join and leave it null.
$stmt = $pdo->prepare(
    "SELECT outbound_caller_id_number
     FROM v_extensions
     WHERE domain_uuid = ?::uuid AND extension = ? LIMIT 1"
);
$stmt->execute([$domain_uuid, $from_ext]);
$ext_row = $stmt->fetch(PDO::FETCH_ASSOC);
error_log("[SMS] ext lookup: domain=$domain_uuid ext=$from_ext outbound_caller_id=" .
    ($ext_row['outbound_caller_id_number'] ?? 'NULL'));

$message_from = !empty($ext_row['outbound_caller_id_number'])
    ? preg_replace('/[^0-9+]/', '', $ext_row['outbound_caller_id_number'])
    : $from_ext;
$user_uuid = null;  // junction table v_extension_users; not required for send

error_log("[SMS] Extension $from_ext -> DID $message_from -> $to (channel: $channel)");

// ── WhatsApp: bypass queue entirely, call send.php directly ──────────────────
// All WhatsApp messages (free-form and template) go straight to send.php
// which calls the Meta Cloud API.  The FusionPBX queue is used for SMS only.
if ($channel === 'whatsapp') {
    $send_php = '/var/www/fusionpbx/app/whatsapp/send.php';
    if ($template_name) {
        $cmd = sprintf(
            "php %s --from=%s --to=%s --template=%s --lang=%s >> /tmp/sms_outbound.log 2>&1",
            $send_php,
            escapeshellarg($message_from),
            escapeshellarg($to),
            escapeshellarg($template_name),
            escapeshellarg($template_lang)
        );
        error_log("[SMS] WhatsApp template: $template_name from $message_from to $to");
    } else {
        $cmd = sprintf(
            "php %s --from=%s --to=%s --message=%s >> /tmp/sms_outbound.log 2>&1",
            $send_php,
            escapeshellarg($message_from),
            escapeshellarg($to),
            escapeshellarg($body)
        );
        error_log("[SMS] WhatsApp free-form from $message_from to $to");
    }
    system($cmd);
    exit(0);
}

// ── Regular flow: provider lookup and queue insertion ─────────────────────────

// Find provider_uuid from v_destinations matching the DID
$stmt = $pdo->prepare(
    "SELECT provider_uuid::text, group_uuid::text FROM v_destinations
     WHERE domain_uuid = ?::uuid AND destination_enabled = 'true' AND provider_uuid IS NOT NULL
     AND ( destination_number = ?
        OR destination_area_code || destination_number = ?
        OR destination_prefix || destination_number = ?
        OR '+' || destination_prefix || destination_number = ? ) LIMIT 1"
);
$stmt->execute([$domain_uuid, $message_from, $message_from, $message_from, $message_from]);
$dest = $stmt->fetch(PDO::FETCH_ASSOC);

// Fallback: find first DID with a provider for this domain
if (empty($dest['provider_uuid'])) {
    error_log("[SMS] No provider for DID $message_from — trying domain fallback");
    $stmt = $pdo->prepare(
        "SELECT provider_uuid::text, group_uuid::text, destination_number
         FROM v_destinations
         WHERE domain_uuid = ?::uuid AND destination_enabled = 'true'
         AND provider_uuid IS NOT NULL LIMIT 1"
    );
    $stmt->execute([$domain_uuid]);
    $dest = $stmt->fetch(PDO::FETCH_ASSOC);
    if (!empty($dest['provider_uuid'])) {
        $message_from = $dest['destination_number'] ?? $message_from;
        error_log("[SMS] Fallback DID: $message_from");
    }
}

if (empty($dest['provider_uuid'])) {
    error_log("[SMS] No provider found for domain $domain_uuid");
    exit(1);
}

// Insert into message queue
$queue_uuid = uuid();
$p = new permissions;
$p->add('message_queue_add', 'temp');

$array['message_queue'][0] = [
    'domain_uuid'        => $domain_uuid,
    'message_queue_uuid' => $queue_uuid,
    'user_uuid'          => $user_uuid,
    'group_uuid'         => $dest['group_uuid'],
    'provider_uuid'      => $dest['provider_uuid'],
    'hostname'           => gethostname(),
    'message_status'     => 'waiting',
    'message_type'       => 'sms',
    'message_direction'  => 'outbound',
    'message_date'       => 'now()',
    'message_from'       => $message_from,
    'message_to'         => $to,
    'message_text'       => $body,
    'message_json'       => 'sms_forced',  // tells message_send_outbound.php to skip WhatsApp auto-routing
];

$db2 = new database;
$db2->app_name = 'messages';
$db2->app_uuid = '4a20815d-042c-47c8-85df-085333e79b87';
$db2->save($array, false);
unset($array);
$p->delete('message_queue_add', 'temp');

// Trigger the outbound sender
$cmd = sprintf(
    "php /var/www/fusionpbx/app/messages/resources/service/message_send_outbound.php 'message_queue_uuid=%s&hostname=%s' >> /tmp/sms_outbound.log 2>&1",
    $queue_uuid, gethostname()
);
system($cmd);
error_log("[SMS] Sent via queue $queue_uuid from $message_from to $to");
