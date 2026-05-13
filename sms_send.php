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
error_log("[SMS] channel='" . ($channel ?: 'auto') . "' to_raw=$to_raw to_digits=$to_digits to=$to");

if (!$from_ext || !$to || !$body || !$from_host) {
    error_log("[SMS] Missing params: from=$from_ext to=$to domain=$from_host");
    exit(1);
}

// Bootstrap FusionPBX
$conf = glob("{/usr/local/etc,/etc}/fusionpbx/config.conf", GLOB_BRACE);
if (empty($conf)) { error_log("[SMS] FusionPBX config not found"); exit(1); }
set_include_path(parse_ini_file($conf[0])['document.root']);
require_once "resources/require.php";

$database = new database;

// Get domain_uuid from domain name
$row = $database->select(
    "SELECT domain_uuid FROM v_domains WHERE domain_name = :domain_name LIMIT 1",
    ['domain_name' => $from_host], 'row'
);
if (empty($row['domain_uuid'])) { error_log("[SMS] Domain not found: $from_host"); exit(1); }
$domain_uuid = $row['domain_uuid'];

// Get outbound_caller_id_number for this extension (the DID to send from)
$row = $database->select(
    "SELECT outbound_caller_id_number, user_uuid FROM v_extensions
     WHERE domain_uuid = :domain_uuid AND extension = :ext LIMIT 1",
    ['domain_uuid' => $domain_uuid, 'ext' => $from_ext], 'row'
);
$message_from = !empty($row['outbound_caller_id_number'])
    ? preg_replace('/[^0-9+]/', '', $row['outbound_caller_id_number'])
    : $from_ext;
$user_uuid = $row['user_uuid'] ?? null;

error_log("[SMS] Extension $from_ext -> DID $message_from -> $to (channel: $channel)");

// ── WhatsApp template shortcut ────────────────────────────────────────────────
// When template= param is present the softphone wants to send a pre-approved
// WhatsApp template (first contact / re-engagement).  Bypass the FusionPBX
// message queue and call send.php directly — no provider lookup needed.
$template_name = trim($args['template'] ?? '');
$template_lang = trim($args['lang']     ?? 'en_US');
if ($template_name && $channel === 'whatsapp') {
    $send_php = '/var/www/fusionpbx/app/whatsapp/send.php';
    $cmd = sprintf(
        "php %s --from=%s --to=%s --template=%s --lang=%s >> /tmp/sms_outbound.log 2>&1",
        $send_php,
        escapeshellarg($message_from),
        escapeshellarg($to),
        escapeshellarg($template_name),
        escapeshellarg($template_lang)
    );
    error_log("[SMS] Template send: $template_name from $message_from to $to");
    system($cmd);
    exit(0);
}

// Find provider_uuid from v_destinations matching the DID
$sql  = "SELECT provider_uuid, group_uuid FROM v_destinations ";
$sql .= "WHERE domain_uuid = :domain_uuid AND destination_enabled = 'true' AND provider_uuid IS NOT NULL ";
$sql .= "AND ( destination_number = :num ";
$sql .= "   OR destination_area_code || destination_number = :num ";
$sql .= "   OR destination_prefix || destination_number = :num ";
$sql .= "   OR '+' || destination_prefix || destination_number = :num ) LIMIT 1";
$dest = $database->select($sql, ['domain_uuid' => $domain_uuid, 'num' => $message_from], 'row');

// Fallback: find first DID with a provider for this domain
if (empty($dest['provider_uuid'])) {
    error_log("[SMS] No provider for DID $message_from — trying domain fallback");
    $dest = $database->select(
        "SELECT provider_uuid, group_uuid FROM v_destinations
         WHERE domain_uuid = :domain_uuid AND destination_enabled = 'true'
         AND provider_uuid IS NOT NULL LIMIT 1",
        ['domain_uuid' => $domain_uuid], 'row'
    );
    if (!empty($dest['provider_uuid'])) {
        // Use this destination's number as message_from
        $did_row = $database->select(
            "SELECT destination_number FROM v_destinations
             WHERE domain_uuid = :domain_uuid AND destination_enabled = 'true'
             AND provider_uuid IS NOT NULL LIMIT 1",
            ['domain_uuid' => $domain_uuid], 'row'
        );
        $message_from = $did_row['destination_number'] ?? $message_from;
        error_log("[SMS] Fallback DID: $message_from");
    }
}

if (empty($dest['provider_uuid'])) {
    error_log("[SMS] No provider found for domain $domain_uuid");
    exit(1);
}

// Insert into message queue
$queue_uuid = uuid();
$p = permissions::new();
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
