# ASEC Android app

React Native + TypeScript Android client for the ASEC MVP. This slice covers
sign-in, session restore, the signed-in home view, and sign-out. It talks only
to the backend API; it never calls Discord or an AI service.

Android only. There is no iOS project, web build, push notification, offline
cache, task/review UI, theming, or release signing in this slice.

## Prerequisites

- Node.js 22.11 or newer (developed on Node 24).
- JDK 17 or newer and the Android SDK, as required by React Native 0.88.
- An Android emulator or a device with USB debugging.

All commands below run from `app/`. `app/` owns its own `package.json` and
lockfile; do not install Node dependencies at the repository root.

## Install

```powershell
cd app
npm install
```

## Configure the API base URL

The committed placeholder is `https://api.invalid.example`. On the first
`npm run <script>`, `scripts/ensure-local-config.js` copies
`src/config.local.example.ts` to the untracked `src/config.local.ts` if it does
not exist. Edit `src/config.local.ts`:

```ts
// Android emulator -> mock server on the host machine
export const API_BASE_URL = 'http://10.0.2.2:8787';

// Physical phone on the same LAN -> mock server on the owner's machine
export const API_BASE_URL = 'http://192.168.1.20:8787';
```

`src/config.local.ts` is git-ignored. Never put a real token, key, or
deployment URL in a committed file.

Cleartext HTTP is allowed only in debug builds and only for `10.0.2.2` (see
`android/app/src/debug/res/xml/network_security_config.xml`). To use a LAN
address, replace that single `<domain>` value and rebuild. Release builds keep
`android:usesCleartextTraffic="false"`.

## Run the mock API

```powershell
npm run mock
```

The server is plain Node.js with no dependencies. It listens on
`127.0.0.1:8787` by default and prints the six-digit OTP for each known roll
number to this console only. Codes and tokens are held in memory and are lost
when the process stops.

| Roll | Role      | Verticals |
| ---- | --------- | --------- |
| R1   | HEAD      | 4, 5      |
| R2   | VOLUNTEER | 2         |
| R3   | AH        | 1, 2      |
| R4   | ADMIN     | none      |

Any other roll number gets the same neutral OTP response but no code is
logged, and verification fails.

Options:

```powershell
node mock-server/server.js --help
node mock-server/server.js --port 9000
node mock-server/server.js --lan            # listen on 0.0.0.0 for a device
$env:MOCK_FAULT = 'wrong-code'; npm run mock
Remove-Item Env:MOCK_FAULT                 # back to normal
```

Fault modes are fixed when the server starts:

| Mode           | Effect                                             |
| -------------- | -------------------------------------------------- |
| `none`         | Normal behaviour.                                  |
| `wrong-code`   | Every `POST /auth/verify` returns 401.             |
| `rate-limit`   | `POST /auth/otp` and `POST /auth/verify` return 429. |
| `unauthorized` | `GET /me` and `POST /auth/logout` return 401.      |

Endpoints:

| Method | Path           | Success                          |
| ------ | -------------- | -------------------------------- |
| POST   | `/auth/otp`    | 202 `{status:"accepted"}`        |
| POST   | `/auth/verify` | 200 `{token,expires_at,person}`  |
| POST   | `/auth/logout` | 204                              |
| GET    | `/me`          | 200 `{person}`                   |
| GET    | `/healthz`     | 200 `{status:"ok"}`              |

## Run the app

Start Metro in one terminal:

```powershell
npm start
```

Then build, install, and launch on a running emulator or connected device:

```powershell
npm run android
```

To build only the debug APK:

```powershell
npm run build:android
```

The APK is written under `android/app/build/outputs/apk/debug/`.

## Checks

```powershell
npm test -- --runInBand
npm run typecheck
npm run lint
```

Tests cover the API client, contract, secure session store, session controller,
sign-in flow, home and restore screens, console/rendered-text secrecy, and the
mock server. No test requires a network connection.

## Security notes

- The session token is stored only through `react-native-keychain`
  (`src/session/keychainSecureStorage.ts`). It is never put in navigation
  state, rendered text, errors, or logs.
- The OTP code is never stored. It is accepted only as exactly six digits.
- The OTP request always shows:
  `If this roll number is registered, a code was sent on Discord.`
- A rejected code shows `Code not accepted`; a 429 shows
  `Too many attempts, try again later`; a network failure keeps the entered
  text and can be retried.
- Sign-out calls `POST /auth/logout`, then clears the local session even if the
  server call fails.
- Any 401 from the API clears the local session through one central hook.
- `GET /me` is the only source of role and verticals. Nothing is trusted from
  the verify response or from local storage.
- `src/api/contract.ts` is the single HTTP contract file, written from
  `docs/api/openapi.json`. Shapes the OpenAPI document does not describe are
  marked `ASSUMED` there.

## Troubleshooting

- **Emulator cannot reach the mock server:** keep the server on
  `127.0.0.1:8787`, set `API_BASE_URL` to `http://10.0.2.2:8787`, and rebuild.
- **Physical device cannot reach it:** start the server with `--lan`, set
  `API_BASE_URL` to the machine's LAN IP, add that IP to the debug network
  security config, and allow the port through the host firewall.
- **Metro cache issues:** `npm start -- --reset-cache`.
- **Android build fails on the JDK:** point `JAVA_HOME` at a JDK 17+ install
  (Android Studio's bundled JBR works) and reopen the terminal.
- **Gradle cannot find the SDK:** set `ANDROID_HOME` for the terminal before
  building:

  ```powershell
  $env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
  $env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
  ```
