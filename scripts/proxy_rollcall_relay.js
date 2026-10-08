/**
 * Mobile Proxy Interceptor & Multi-Account Rollcall Relay Script
 *
 * Supported Clients:
 * - Shadowrocket (小火箭)
 * - Surge 4 / 5
 * - Loon
 * - Quantumult X
 * - Reqable (via custom rewrite/proxy)
 *
 * Description:
 * When you scan a QR code using the official mobile app, your phone sends an HTTP PUT to:
 * /api/rollcall/{id}/answer_qr_rollcall
 *
 * This script transparently intercepts that request, extracts the rollcall ID and token,
 * forwards it in the background to your auto-rollcall service, and signs attendance for
 * all configured accounts simultaneously.
 *
 * Configuration for Shadowrocket (HTTPS Decryption / MitM required):
 * ---------------------------------------------------------------------------------
 * [MITM]
 * hostname = %APPEND% *tronclass*
 *
 * [Script]
 * RollcallRelay = type=http-request,pattern=^https?:\/\/.*\/api\/rollcall\/[0-9]+\/answer.*,requires-body=1,max-size=0,script-path=https://tronclass-bot.onrender.com/proxy.js
 * ---------------------------------------------------------------------------------
 *
 * Configuration for Surge:
 * ---------------------------------------------------------------------------------
 * [MITM]
 * hostname = %APPEND% *tronclass*
 *
 * [Script]
 * RollcallRelay = type=http-request,pattern=^https?:\/\/.*\/api\/rollcall\/[0-9]+\/answer.*,requires-body=1,script-path=https://tronclass-bot.onrender.com/proxy.js
 * ---------------------------------------------------------------------------------
 *
 * Configuration for Loon:
 * ---------------------------------------------------------------------------------
 * [MITM]
 * hostname = *tronclass*
 *
 * [Script]
 * http-request ^https?:\/\/.*\/api\/rollcall\/[0-9]+\/answer.* script-path=https://tronclass-bot.onrender.com/proxy.js, requires-body=true, tag=RollcallRelay
 * ---------------------------------------------------------------------------------
 *
 * Configuration for Quantumult X:
 * ---------------------------------------------------------------------------------
 * [mitm]
 * hostname = *tronclass*
 *
 * [rewrite_local]
 * ^https?:\/\/.*\/api\/rollcall\/[0-9]+\/answer.* url script-request-body https://tronclass-bot.onrender.com/proxy.js
 * ---------------------------------------------------------------------------------
 */

(function () {
    const DEFAULT_BACKEND = "https://tronclass-bot.onrender.com";
    const BACKEND_URL = ("__BACKEND_URL__".startsWith("http") ? "__BACKEND_URL__" : DEFAULT_BACKEND) + "/api/submit";

    const reqUrl = (typeof $request !== "undefined" && $request.url) ? $request.url : "";
    const method = (typeof $request !== "undefined" && $request.method) ? $request.method.toUpperCase() : "PUT";
    const rawBody = (typeof $request !== "undefined" && $request.body) ? $request.body : "";

    // Always ensure the native request is not blocked or modified
    function finish() {
        if (typeof $done !== "undefined") {
            $done({});
        }
    }

    if (!rawBody || (method !== "PUT" && method !== "POST")) {
        finish();
        return;
    }

    let payloadData = {};
    try {
        if (typeof rawBody === "string") {
            payloadData = JSON.parse(rawBody);
        } else if (typeof rawBody === "object") {
            payloadData = rawBody;
        }
    } catch (e) {
        payloadData = { data: String(rawBody) };
    }

    // Extract rollcallId from URL path if not already in JSON body
    const match = reqUrl.match(/\/api\/rollcall\/([0-9]+)\//);
    if (match && match[1]) {
        payloadData.rollcallId = match[1];
    }

    const postPayload = {
        payload: JSON.stringify(payloadData),
        profile: "all",
        source: "mobile_proxy_interceptor"
    };

    const requestOptions = {
        url: BACKEND_URL,
        headers: {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "RollcallProxyRelay/1.0"
        },
        body: JSON.stringify(postPayload),
        timeout: 6000
    };

    // Surge / Shadowrocket / Loon HTTP Client
    if (typeof $httpClient !== "undefined" && typeof $httpClient.post === "function") {
        $httpClient.post(requestOptions, function (error, response, data) {
            if (error) {
                console.log("[Proxy Relay Error]: " + error);
            } else {
                console.log("[Proxy Relay Success]: " + (data || "status=" + (response ? response.status : "unknown")));
            }
        });
    }
    // Quantumult X HTTP Client
    else if (typeof $task !== "undefined" && typeof $task.fetch === "function") {
        $task.fetch({
            url: requestOptions.url,
            method: "POST",
            headers: requestOptions.headers,
            body: requestOptions.body
        }).then(
            function (response) {
                console.log("[QX Relay Success]: " + (response ? response.body : "done"));
            },
            function (error) {
                console.log("[QX Relay Error]: " + error);
            }
        );
    } else {
        console.log("[Proxy Relay]: HTTP client environment not detected.");
    }

    // Unblock original app request immediately
    finish();
})();
