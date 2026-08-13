/* api-keys.js — Lightweight API key UI helpers.
 *
 * PHASE-3-FIX (2026-08-13): Most of the API key UI is already in app-nav.js.
 * This file provides additional utilities: clipboard helper, key formatting,
 * and a small in-page verifier.
 */

(function() {
    'use strict';

    // Format an API key for display (mask the middle).
    window.formatApiKey = function(key) {
        if (!key || key.length < 16) return key;
        return key.slice(0, 12) + '…' + key.slice(-4);
    };

    // Verify a key from the URL (e.g. user pasted ?api_key=... to test)
    window.verifyApiKeyFromUrl = async function() {
        const params = new URLSearchParams(window.location.search);
        const key = params.get('api_key');
        if (!key) return null;
        try {
            const res = await fetch('/api/keys/verify', {
                headers: { 'Authorization': 'Bearer ' + key }
            });
            return await res.json();
        } catch (e) {
            return { valid: false, reason: e.message };
        }
    };

    // If the URL contains ?api_key=..., show a small banner with the verification result.
    async function showApiKeyBanner() {
        const params = new URLSearchParams(window.location.search);
        const key = params.get('api_key');
        if (!key) return;

        const result = await window.verifyApiKeyFromUrl();
        if (!result) return;

        const banner = document.createElement('div');
        banner.style.cssText = `
            position:fixed; top:0; left:0; right:0; z-index:2000;
            padding:8px 16px; font-size:13px; font-weight:600;
            background:${result.valid ? 'rgba(0,200,83,0.95)' : 'rgba(255,59,48,0.95)'};
            color:#1a1a1a; text-align:center;
        `;
        banner.textContent = result.valid
            ? `✓ API key valid: ${result.label} (${result.key_prefix}…) — ${result.total_requests} requests so far`
            : `✗ API key invalid: ${result.reason}`;
        document.body.appendChild(banner);
        setTimeout(() => {
            banner.style.transition = 'opacity 0.5s';
            banner.style.opacity = '0';
            setTimeout(() => banner.remove(), 500);
        }, 5000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showApiKeyBanner);
    } else {
        showApiKeyBanner();
    }
})();
