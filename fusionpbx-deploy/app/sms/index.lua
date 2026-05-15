-- ============================================================
-- FreeSWITCH SIP MESSAGE chatplan dispatcher for SMS + WhatsApp
-- ============================================================
-- Deploy path: /usr/share/freeswitch/scripts/app/sms/index.lua
--
-- What it does:
--   1. Receives outbound SIP MESSAGE from a softphone (chatplan calls
--      `app.lua sms` which dispatches here).
--   2. Filters out inbound/external messages (only short extension numbers
--      are real outbound — anything 7+ digits is an inbound message we ignore).
--   3. Decides channel:
--        10-digit recipient (e.g. 3055551234)  → channel=sms
--        11-digit with leading 1 (1XXXXXXXXXX) → channel=whatsapp
--   4. Detects a special WhatsApp template marker in the body:
--        __TEMPLATE__:<template_name>:<lang_code>
--      and forwards it as &template=<name>&lang=<code> to send.php.
--   5. Shells out to send.php with all parameters URL-encoded.
--
-- The iOS / Android softphone wraps WhatsApp template requests by sending
-- a body of:   __TEMPLATE__:initial_contact:en
-- when the user is starting a new conversation (no prior inbound from peer).

local from_user = message:getHeader("from_user")
local from_host = message:getHeader("from_host")
local to_user   = message:getHeader("to_user")
local body      = message:getBody()

freeswitch.consoleLog("WARNING", string.format(
    "[SMS] DEBUG to_user=[%s] from_user=[%s]\n",
    tostring(to_user), tostring(from_user)))

-- Only process OUTBOUND messages from local extensions (short numbers).
-- Anything 7+ digits is an inbound message we don't bridge.
if not from_user or #from_user >= 7 then
    freeswitch.consoleLog("DEBUG", string.format(
        "[SMS] Skipping inbound/external message from %s\n", tostring(from_user)))
    return
end

if to_user and body and from_host then
    local to_digits = to_user:gsub("[^0-9]", "")
    local channel = "sms"
    if #to_digits == 11 and to_digits:sub(1,1) == "1" then
        channel = "whatsapp"
    end

    -- Detect a WhatsApp template request — body format:
    --   __TEMPLATE__:<template_name>:<lang_code>
    -- e.g. __TEMPLATE__:initial_contact:en_US
    local extra_params = ""
    if channel == "whatsapp" then
        local tmpl_name, tmpl_lang = body:match("^__TEMPLATE__:([%w_]+):([%w_]+)$")
        if tmpl_name then
            extra_params = string.format(
                "&template=%s&lang=%s", tmpl_name, tmpl_lang or "en_US")
            freeswitch.consoleLog("INFO", string.format(
                "[SMS] WhatsApp template request: %s to %s\n",
                tmpl_name, to_digits))
        end
    end

    -- Escape single quotes in body (the only argument the user controls).
    local escaped_body = body:gsub("'", "'\\''")

    local cmd = string.format(
        "php /usr/share/freeswitch/scripts/app/sms/send.php "
     .. "'from=%s&domain=%s&to=%s&body=%s&channel=%s%s' "
     .. ">> /tmp/sms_outbound.log 2>&1",
        from_user, from_host, to_user, escaped_body, channel, extra_params)

    freeswitch.consoleLog("DEBUG", string.format(
        "[SMS] Executing channel=%s to=%s\n", channel, to_user))
    os.execute(cmd)
else
    freeswitch.consoleLog("WARNING", string.format(
        "[SMS] Missing params - from:%s to:%s\n",
        tostring(from_user), tostring(to_user)))
end
