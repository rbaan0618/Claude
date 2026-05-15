<?php
/*
 * WhatsApp Cloud API Webhook Handler for FusionPBX (Multi-Tenant Production)
 * ==========================================================================
 * Deploy path:   /var/www/fusionpbx/app/whatsapp/webhook.php
 * Public URL:    https://<fusionpbx-host>/app/whatsapp/webhook.php
 *
 * What it does:
 *   • Verifies Meta webhook subscription (GET handshake)
 *   • Receives inbound WhatsApp messages (POST)
 *   • Auto-marks each message as READ on WhatsApp (blue checkmarks)
 *   • Looks up the FusionPBX tenant (domain) for the receiving DID
 *   • Inserts message into v_messages       (visible in FusionPBX GUI)
 *   • Inserts message into v_message_queue  (delivered to softphones via SIP MESSAGE)
 *
 * Configuration in Meta Business Manager:
 *   WhatsApp -> Configuration -> Webhook
 *     Callback URL:   https://<fusionpbx-host>/app/whatsapp/webhook.php
 *     Verify Token:   $verify_token below (must match exactly)
 *     Subscribed:     messages, message_status
 *
 * Version: 1.0 Production
 * Date:    April 2026
 */

// ============================================================
// CONFIGURATION  — FILL IN BEFORE DEPLOY
// ============================================================

// Verify token — must match exactly what you set in Meta Business Manager.
// Generate a random one with: openssl rand -hex 32
$verify_token = 'YOUR_WEBHOOK_VERIFY_TOKEN_HERE';

$log_file = '/var/log/whatsapp_webhook.log';

// Database connection. Get values from /etc/fusionpbx/config.conf.
$db_host = '127.0.0.1';
$db_name = 'fusionpbx';
$db_user = 'fusionpbx';
$db_pass = 'YOUR_FUSIONPBX_DB_PASSWORD_HERE';

// SMS provider UUID used for queue delivery (FusionPBX needs to know which
// "provider" to attribute inbound WhatsApp messages to so the message_queue
// service knows where to route them).
//   Find yours with:
//     psql -U fusionpbx -d fusionpbx -c \
//         "SELECT message_provider_uuid, message_provider_name FROM v_message_providers;"
$sms_provider_uuid = 'YOUR_SMS_PROVIDER_UUID_HERE';

// ============================================================
// CLIENT NUMBERS AND ACCESS TOKENS  — FILL IN
// ============================================================
// Each WhatsApp Business number needs its own access token and phone_number_id.
// These MUST match the values in /var/www/fusionpbx/app/whatsapp/send.php.

$clients = [
    'PHONE_NUMBER_WITHOUT_PLUS_1' => [
        'name'     => 'Client A',
        'phone_id' => 'META_PHONE_NUMBER_ID_FOR_A',
        'token'    => 'META_ACCESS_TOKEN_FOR_A',
    ],
    // Add additional client numbers as needed.
];

// Default token (used if the receiving number isn't found in $clients).
$default_token    = 'META_ACCESS_TOKEN_DEFAULT';
$default_phone_id = 'META_PHONE_NUMBER_ID_DEFAULT';

// ============================================================
// WEBHOOK VERIFICATION (GET request from Meta)
// ============================================================
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $mode      = $_GET['hub_mode']         ?? '';
    $token     = $_GET['hub_verify_token'] ?? '';
    $challenge = $_GET['hub_challenge']    ?? '';

    if ($mode === 'subscribe' && $token === $verify_token) {
        wa_log($log_file, "Webhook verified successfully");
        http_response_code(200);
        echo $challenge;
        exit;
    } else {
        wa_log($log_file, "Webhook verification failed - bad token");
        http_response_code(403);
        echo "Forbidden";
        exit;
    }
}

// ============================================================
// INCOMING MESSAGE HANDLER (POST request from Meta)
// ============================================================
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $payload = file_get_contents('php://input');
    $data    = json_decode($payload, true);

    // Respond 200 immediately so Meta doesn't retry. Process below.
    http_response_code(200);

    wa_log($log_file, "Received webhook: " . substr($payload, 0, 2000));

    if (!isset($data['entry'])) {
        exit;
    }

    // Connect to database once.
    try {
        $pdo = new PDO(
            "pgsql:host=$db_host;dbname=$db_name",
            $db_user,
            $db_pass,
            [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
        );
    } catch (PDOException $e) {
        wa_log($log_file, "Database connection error: " . $e->getMessage());
        exit;
    }

    foreach ($data['entry'] as $entry) {
        if (!isset($entry['changes'])) continue;

        foreach ($entry['changes'] as $change) {
            if ($change['field'] !== 'messages') continue;

            $value           = $change['value'];
            $metadata        = $value['metadata'] ?? [];
            $business_number = $metadata['display_phone_number'] ?? '';
            $phone_number_id = $metadata['phone_number_id']      ?? '';

            $business_number_clean = preg_replace('/[^0-9]/', '', $business_number);

            // Get the correct access token for this number.
            $client = $clients[$business_number_clean] ?? null;
            $token  = $client['token'] ?? $default_token;

            wa_log($log_file, "Business number: $business_number (ID: $phone_number_id) Client: " . ($client['name'] ?? 'default'));

            // Find which FusionPBX domain/tenant this DID belongs to.
            $tenant = find_tenant($pdo, $business_number_clean);

            if (!$tenant) {
                // Retry without country code (strip leading 1 for US numbers).
                if (strlen($business_number_clean) === 11 && substr($business_number_clean, 0, 1) === '1') {
                    $tenant = find_tenant($pdo, substr($business_number_clean, 1));
                }
            }

            if (!$tenant) {
                wa_log($log_file, "WARNING: No tenant/destination found for $business_number_clean");
            }

            // Process inbound messages.
            if (isset($value['messages'])) {
                foreach ($value['messages'] as $message) {
                    process_message($pdo, $message, $business_number_clean, $tenant, $token, $phone_number_id, $log_file, $sms_provider_uuid);
                }
            }

            // Process status updates (delivery, read receipts) — log only.
            if (isset($value['statuses'])) {
                foreach ($value['statuses'] as $status) {
                    $recipient  = $status['recipient_id'] ?? '';
                    $msg_status = $status['status']       ?? '';
                    $msg_id     = $status['id']           ?? '';
                    wa_log($log_file, "Status: $msg_status for $recipient (number: $business_number_clean)");
                }
            }
        }
    }
    exit;
}

// ============================================================
// FUNCTIONS
// ============================================================

/**
 * Find the FusionPBX tenant (domain) for a given phone number.
 *
 * Tries two strategies:
 *  1. Match the number against v_destinations (inbound DIDs).
 *  2. Match against v_extensions.outbound_caller_id_number.
 */
function find_tenant($pdo, $number) {
    // 1. v_destinations match
    $stmt = $pdo->prepare("
        SELECT d.domain_uuid, d.domain_name, dest.destination_number
        FROM v_destinations dest
        JOIN v_domains d ON d.domain_uuid = dest.domain_uuid
        WHERE replace(replace(dest.destination_number, '+', ''), '-', '') = :did
           OR dest.destination_number = :did2
           OR dest.destination_number = :did3
        LIMIT 1
    ");
    $stmt->execute([
        'did'  => $number,
        'did2' => '+' . $number,
        'did3' => $number,
    ]);
    $result = $stmt->fetch(PDO::FETCH_ASSOC);
    if ($result) return $result;

    // 2. v_extensions outbound CID match
    $stmt = $pdo->prepare("
        SELECT d.domain_uuid, d.domain_name, e.extension, e.outbound_caller_id_number
        FROM v_extensions e
        JOIN v_domains d ON d.domain_uuid = e.domain_uuid
        WHERE replace(replace(e.outbound_caller_id_number, '+', ''), '-', '') = :did
        LIMIT 1
    ");
    $stmt->execute(['did' => $number]);
    $result = $stmt->fetch(PDO::FETCH_ASSOC);

    return $result ?: null;
}

/**
 * Process a single incoming WhatsApp message.
 */
function process_message($pdo, $message, $business_number, $tenant, $token, $phone_number_id, $log_file, $sms_provider_uuid) {
    $from      = $message['from']      ?? '';
    $msg_id    = $message['id']        ?? '';
    $timestamp = $message['timestamp'] ?? time();
    $type      = $message['type']      ?? 'unknown';

    // Extract text based on message type — Meta supports many types.
    $text = '';
    switch ($type) {
        case 'text':
            $text = $message['text']['body'] ?? '';
            break;
        case 'image':
            $text = '[Image] ' . ($message['image']['caption'] ?? '');
            break;
        case 'document':
            $text = '[Document] ' . ($message['document']['filename'] ?? '');
            break;
        case 'audio':
            $text = '[Audio message]';
            break;
        case 'video':
            $text = '[Video] ' . ($message['video']['caption'] ?? '');
            break;
        case 'location':
            $lat  = $message['location']['latitude']  ?? '';
            $lon  = $message['location']['longitude'] ?? '';
            $name = $message['location']['name']      ?? '';
            $text = "[Location] $name $lat, $lon";
            break;
        case 'contacts':
            $contact_name = $message['contacts'][0]['name']['formatted_name'] ?? 'Unknown';
            $text = "[Contact] $contact_name";
            break;
        case 'reaction':
            $text = '[Reaction] ' . ($message['reaction']['emoji'] ?? '');
            break;
        case 'sticker':
            $text = '[Sticker]';
            break;
        case 'button':
            $text = '[Button] ' . ($message['button']['text'] ?? '');
            break;
        case 'interactive':
            $interactive_type = $message['interactive']['type'] ?? '';
            if ($interactive_type === 'button_reply') {
                $text = '[Reply] ' . ($message['interactive']['button_reply']['title'] ?? '');
            } elseif ($interactive_type === 'list_reply') {
                $text = '[List] ' . ($message['interactive']['list_reply']['title'] ?? '');
            }
            break;
        default:
            $text = "[Unsupported: $type]";
    }

    $tenant_name = $tenant['domain_name'] ?? 'unknown';
    wa_log($log_file, "[$tenant_name] Message from +$from: $text (type: $type)");

    // Mark message as read on WhatsApp (blue checkmarks).
    mark_as_read($token, $phone_number_id, $msg_id);

    // Inject into FusionPBX for storage + softphone delivery.
    if ($tenant) {
        inject_to_fusionpbx($pdo, $from, $business_number, $text, $type, $timestamp, $tenant, $log_file, $sms_provider_uuid);
    }
}

/**
 * Mark a WhatsApp message as read.
 */
function mark_as_read($token, $phone_id, $message_id) {
    if (empty($token) || empty($phone_id)) return;

    $url  = "https://graph.facebook.com/v23.0/$phone_id/messages";
    $data = json_encode([
        'messaging_product' => 'whatsapp',
        'status'            => 'read',
        'message_id'        => $message_id,
    ]);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => $data,
        CURLOPT_HTTPHEADER     => [
            "Authorization: Bearer $token",
            "Content-Type: application/json",
        ],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 5,
    ]);
    curl_exec($ch);
    curl_close($ch);
}

/**
 * Insert the received WhatsApp message into FusionPBX:
 *   • v_messages       — visible in the Messages GUI
 *   • v_message_queue  — delivered to softphones/desk phones via the
 *                        message_queue service (which writes a SIP MESSAGE
 *                        to the active registration).
 */
function inject_to_fusionpbx($pdo, $from, $to, $text, $type, $timestamp, $tenant, $log_file, $sms_provider_uuid) {
    try {
        $from_clean   = ltrim($from, '+');
        $to_clean     = preg_replace('/[^0-9]/', '', $to);
        $domain_uuid  = $tenant['domain_uuid'];
        $message_uuid = gen_uuid();

        // ── INSERT 1: v_messages (GUI) ──
        $stmt = $pdo->prepare("
            INSERT INTO v_messages (
                message_uuid, domain_uuid,
                message_type, message_direction,
                message_from, message_to,
                message_text, message_json,
                message_date, message_read, insert_date
            ) VALUES (
                :uuid, :domain_uuid,
                'sms', 'inbound',
                :from_num, :to_num,
                :text, :json,
                to_timestamp(:ts), false, now()
            )
        ");
        $stmt->execute([
            'uuid'        => $message_uuid,
            'domain_uuid' => $domain_uuid,
            'from_num'    => '+' . $from_clean,
            'to_num'      => '+' . $to_clean,
            'text'        => $text,
            'json'        => json_encode(['type' => $type, 'source' => 'whatsapp']),
            'ts'          => $timestamp,
        ]);

        // ── Find user_uuid for the destination ──
        //
        // Method 1: dialplan destination -> extension -> user mapping
        $stmt3 = $pdo->prepare("
            SELECT e.extension, eu.user_uuid
            FROM v_destinations d
            JOIN v_extensions e ON e.domain_uuid = d.domain_uuid
                AND d.destination_actions::text LIKE '%' || e.extension || ' XML%'
            JOIN v_extension_users eu ON eu.extension_uuid = e.extension_uuid
            WHERE d.domain_uuid = :domain_uuid
            AND replace(replace(d.destination_number, '+', ''), '-', '') = :did
            LIMIT 1
        ");
        $stmt3->execute([
            'domain_uuid' => $domain_uuid,
            'did'         => $to_clean,
        ]);
        $ext_user  = $stmt3->fetch(PDO::FETCH_ASSOC);
        $user_uuid = $ext_user['user_uuid'] ?? null;

        // Method 2 (fallback): first available user in the domain.
        if (empty($user_uuid)) {
            $stmt4 = $pdo->prepare("
                SELECT eu.user_uuid
                FROM v_extension_users eu
                JOIN v_extensions e ON e.extension_uuid = eu.extension_uuid
                WHERE e.domain_uuid = :domain_uuid
                AND e.enabled = 'true'
                ORDER BY e.extension
                LIMIT 1
            ");
            $stmt4->execute(['domain_uuid' => $domain_uuid]);
            $fallback  = $stmt4->fetch(PDO::FETCH_ASSOC);
            $user_uuid = $fallback['user_uuid'] ?? null;
        }

        // ── INSERT 2: v_message_queue (delivery) ──
        $queue_uuid = gen_uuid();
        $hostname   = gethostname();
        $queue_json = json_encode([
            'to'      => $to_clean,
            'from'    => '+' . $from_clean,
            'type'    => 'sms',
            'message' => $text,
        ]);

        $stmt2 = $pdo->prepare("
            INSERT INTO v_message_queue (
                message_queue_uuid, domain_uuid, provider_uuid,
                user_uuid, hostname, message_type,
                message_status, message_direction,
                message_date, message_from, message_to,
                message_text, message_json, insert_date
            ) VALUES (
                :uuid, :domain_uuid, :provider_uuid,
                :user_uuid, :hostname, 'sms',
                'waiting', 'inbound',
                to_timestamp(:ts), :from_num, :to_num,
                :text, :json, now()
            )
        ");
        $stmt2->execute([
            'uuid'          => $queue_uuid,
            'domain_uuid'   => $domain_uuid,
            'provider_uuid' => $sms_provider_uuid,
            'user_uuid'     => $user_uuid,
            'hostname'      => $hostname,
            'ts'            => $timestamp,
            'from_num'      => '+' . $from_clean,
            'to_num'        => $to_clean,
            'text'          => $text,
            'json'          => $queue_json,
        ]);

        wa_log('/var/log/whatsapp_webhook.log',
            "Message injected: $message_uuid | Queue: $queue_uuid | Tenant: {$tenant['domain_name']} | User: " . ($user_uuid ?? 'none'));

    } catch (PDOException $e) {
        wa_log('/var/log/whatsapp_webhook.log',
            "Database error: " . $e->getMessage());
    }
}

/** Generate a UUID v4. */
function gen_uuid() {
    return sprintf('%04x%04x-%04x-%04x-%04x-%04x%04x%04x',
        mt_rand(0, 0xffff), mt_rand(0, 0xffff),
        mt_rand(0, 0xffff),
        mt_rand(0, 0x0fff) | 0x4000,
        mt_rand(0, 0x3fff) | 0x8000,
        mt_rand(0, 0xffff), mt_rand(0, 0xffff), mt_rand(0, 0xffff)
    );
}

/** Simple log helper. */
function wa_log($file, $message) {
    $timestamp = date('Y-m-d H:i:s');
    file_put_contents($file, "[$timestamp] $message\n", FILE_APPEND | LOCK_EX);
}
