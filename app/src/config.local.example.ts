/**
 * Local API configuration example.
 *
 * `npm run <script>` copies this file to `src/config.local.ts` when that file
 * does not exist yet. `src/config.local.ts` is untracked, so each developer can
 * point the app at the mock server or the real backend without committing a
 * URL.
 *
 * The committed value is a deliberately fake placeholder. It must never be a
 * real deployment URL, token, key or Firebase file.
 *
 * Android emulator -> mock server on the host machine:
 *   export const API_BASE_URL = 'http://10.0.2.2:8787';
 *
 * Physical phone on the same LAN -> mock server on the owner's machine:
 *   export const API_BASE_URL = 'http://<owner-lan-ip>:8787';
 *   Also change the single <domain> in
 *   android/app/src/debug/res/xml/network_security_config.xml.
 */
export const API_BASE_URL = 'https://api.invalid.example';
