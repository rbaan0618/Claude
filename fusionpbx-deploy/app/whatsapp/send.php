<?php
/*
 * WhatsApp Cloud API — Send Outbound Messages (Multi-Tenant Production)
 * ====================================================================
 * Deploy path:  /var/www/fusionpbx/app/whatsapp/send.php
 * Called from:  CLI (manual test) or /usr/share/freeswitch/scripts/app/sms/send.php
 *               when channel=whatsapp is detected in the SIP MESSAGE bridge.
 *
 * Usage:
 *   php send.php --from=13053561411 --to=5215512345678 --message="Hello"
 *   php send.php --from=13053561411 --to=5215512345678 --template=hello_world --lang=en_US
 *   php send.php --from=13053561411 --to=5215512345678 --template=call_perm --type=voice_call
 *   php send.php --from=13053561411 --to=5215512345678 --image=URL --message="Caption"
 *   php send.php --list                  (show all configured numbers)
 *   php send.php --list --verbose        (show numbers with token preview)
 *
 * Version: 1.0 Production
 * Date:    April 2026
 */

// ============================================================
// CLIENT NUMBERS AND CREDENTIALS  — FILL IN BEFORE DEPLOY
// ============================================================
// Each WhatsApp Business number needs its own access token and phone_number_id.
//   - Phone Number ID: Meta Developer Dashboard → API Setup page
//   - Token:           Meta Business Settings → System Users → Generate Token
//
// If all numbers are under YOUR Meta Business Account, they can share the same
// token. If clients have their own Meta accounts, each gets a different token.
//
// Generate a permanent (non-expiring) token via a System User — short-lived
// user tokens expire after 24 hours and will silently break production.

$clients = [
    'PHONE_NUMBER_WITHOUT_PLUS_1' => [
        'name'     => 'Client A',
        'phone_id' => 'META_PHONE_NUMBER_ID_FOR_A',
        'token'    => 'META_ACCESS_TOKEN_FOR_A',
    ],
    // Add additional client numbers:
    // 'PHONE_NUMBER_WITHOUT_PLUS_2' => [
    //     'name'     => 'Client B',
    //     'phone_id' => 'META_PHONE_NUMBER_ID_FOR_B',
    //     'token'    => 'META_ACCESS_TOKEN_FOR_B',
    // ],
];

$log_file = '/var/log/whatsapp_send.log';

// ============================================================
// PARSE ARGUMENTS
// ============================================================
$options = getopt('', ['from:', 'to:', 'message:', 'template:', 'lang:', 'type:', 'image:', 'list', 'verbose']);

// --list: print all configured numbers
if (isset($options['list'])) {
    $verbose = isset($options['verbose']);
    echo "\nConfigured WhatsApp Business Numbers:\n";
    echo str_repeat('-', 70) . "\n";
    printf("  %-15s %-25s %-20s\n", "Number", "Name", "Phone ID");
    echo str_repeat('-', 70) . "\n";
    foreach ($clients as $number => $info) {
        printf("  +%-14s %-25s %-20s\n", $number, $info['name'], $info['phone_id']);
        if ($verbose) {
            echo "                 Token: " . substr($info['token'], 0, 20) . "...\n";
        }
    }
    echo str_repeat('-', 70) . "\n";
    echo "Total: " . count($clients) . " number(s)\n\n";
    exit(0);
}

$from     = $options['from']     ?? null;
$to       = $options['to']       ?? null;
$message  = $options['message']  ?? null;
$template = $options['template'] ?? null;
$lang     = $options['lang']     ?? 'en_US';
$type     = $options['type']     ?? null;
$image    = $options['image']    ?? null;

// Validate required params
if (!$from || !$to) {
    echo "\n";
    echo "WhatsApp Multi-Tenant Sender for FusionPBX\n";
    echo str_repeat('=', 50) . "\n";
    echo "\n";
    echo "Usage:\n";
    echo "  Text:           php send.php --from=NUMBER --to=NUMBER --message=\"text\"\n";
    echo "  Template:       php send.php --from=NUMBER --to=NUMBER --template=name --lang=en_US\n";
    echo "  Call permission:php send.php --from=NUMBER --to=NUMBER --template=name --type=voice_call\n";
    echo "  Image:          php send.php --from=NUMBER --to=NUMBER --image=URL --message=\"caption\"\n";
    echo "  List numbers:   php send.php --list\n";
    echo "  List (verbose): php send.php --list --verbose\n";
    echo "\n";
    echo "  --from = Business number SENDING the message (must be configured in \$clients)\n";
    echo "  --to   = WhatsApp recipient (with country code, no +)\n";
    echo "\n";
    echo "Notes:\n";
    echo "  - Template messages can be sent anytime (re-opens conversation window).\n";
    echo "  - Free-form text/image messages only work within 24h conversation window.\n";
    echo "  - The 24h window opens when the recipient messages you first or replies.\n";
    echo "  - Outbound calling is blocked to US, Canada, Egypt, Vietnam, Nigeria.\n";
    echo "\n";
    exit(1);
}

// Clean the from number
$from_clean = preg_replace('/[^0-9]/', '', $from);

// Find the client config
if (!isset($clients[$from_clean])) {
    echo "Error: Number +$from_clean is not configured.\n";
    echo "Run 'php send.php --list' to see configured numbers.\n";
    echo "Add the number to the \$clients array in send.php.\n";
    exit(1);
}

$client          = $clients[$from_clean];
$access_token    = $client['token'];
$phone_number_id = $client['phone_id'];
$client_name     = $client['name'];

$graph_url = "https://graph.facebook.com/v23.0/$phone_number_id/messages";

echo "Sending as: $client_name (+$from_clean)\n";

// ============================================================
// BUILD PAYLOAD
// ============================================================
$payload = ['messaging_product' => 'whatsapp', 'to' => $to];

if ($template) {
    // Template message — can be sent anytime, doesn't need 24hr window.
    $payload['type'] = 'template';
    $payload['template'] = [
        'name'     => $template,
        'language' => ['code' => $lang],
    ];

    // Add a voice_call button for call-permission templates.
    if ($type === 'voice_call') {
        $payload['template']['components'] = [[
            'type'       => 'button',
            'sub_type'   => 'voice_call',
            'index'      => 0,
            'parameters' => [],
        ]];
    }
} elseif ($image) {
    // Image message — requires 24hr conversation window.
    $payload['type']  = 'image';
    $payload['image'] = [
        'link'    => $image,
        'caption' => $message ?? '',
    ];
} elseif ($message) {
    // Text message — requires 24hr conversation window.
    $payload['type'] = 'text';
    $payload['text'] = [
        'preview_url' => true,
        'body'        => $message,
    ];
} else {
    echo "Error: Must provide --message, --template, or --image\n";
    exit(1);
}

// ============================================================
// SEND REQUEST
// ============================================================
$json = json_encode($payload);

$ch = curl_init($graph_url);
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => $json,
    CURLOPT_HTTPHEADER     => [
        "Authorization: Bearer $access_token",
        "Content-Type: application/json",
    ],
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => 30,
]);

$response   = curl_exec($ch);
$http_code  = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curl_error = curl_error($ch);
curl_close($ch);

// ============================================================
// HANDLE RESPONSE
// ============================================================
$result    = json_decode($response, true);
$timestamp = date('Y-m-d H:i:s');

if ($http_code === 200 && isset($result['messages'][0]['id'])) {
    $msg_id = $result['messages'][0]['id'];
    echo "Message sent successfully\n";
    echo "  From: +$from_clean ($client_name)\n";
    echo "  To: $to\n";
    echo "  Type: " . ($template ? "template:$template" : ($image ? 'image' : 'text')) . "\n";
    echo "  Message ID: $msg_id\n";
    file_put_contents($log_file,
        "[$timestamp] SENT from=+$from_clean ($client_name) to=$to type=" . ($template ?? 'text') . " msg_id=$msg_id\n",
        FILE_APPEND | LOCK_EX);
} else {
    $error_msg  = $result['error']['message'] ?? $curl_error ?? 'Unknown error';
    $error_code = $result['error']['code']    ?? $http_code;
    echo "Failed to send message\n";
    echo "  From: +$from_clean ($client_name)\n";
    echo "  To: $to\n";
    echo "  Error: $error_msg\n";
    echo "  Code: $error_code\n";

    // Helpful hints for common Meta errors.
    if ($error_code == 131047) {
        echo "  Hint: 24-hour conversation window expired. Send a template message first.\n";
    } elseif ($error_code == 133010) {
        echo "  Hint: Number not registered. Run: curl -X POST https://graph.facebook.com/v23.0/$phone_number_id/register ...\n";
    } elseif ($error_code == 190) {
        echo "  Hint: Access token invalid or expired. Regenerate via Meta Business Settings -> System Users.\n";
    }

    file_put_contents($log_file,
        "[$timestamp] FAILED from=+$from_clean ($client_name) to=$to error=$error_msg code=$error_code\n",
        FILE_APPEND | LOCK_EX);
}
