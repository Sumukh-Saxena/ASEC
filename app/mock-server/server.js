/* eslint-env node */

'use strict';

/**
 * ASEC mock API for local app development.
 *
 * Plain Node.js, no dependencies, no database. It implements only the five
 * endpoints the app needs for this slice:
 *
 *   POST /auth/otp      -> 202 {status:"accepted"} (always neutral)
 *   POST /auth/verify   -> 200 {token,expires_at,person} or 401
 *   POST /auth/logout   -> 204 or 401
 *   GET  /me            -> 200 {person} or 401
 *   GET  /healthz       -> 200 {status:"ok"}
 *
 * OTP codes are printed to the console and never returned in a response.
 * Tokens, request bodies and Authorization headers are never logged.
 *
 * Fault modes (MOCK_FAULT or --fault):
 *   none          normal behaviour
 *   wrong-code    every /auth/verify call returns 401
 *   rate-limit    /auth/otp and /auth/verify return 429
 *   unauthorized  /me and /auth/logout return 401
 */

const http = require('node:http');
const crypto = require('node:crypto');

const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_PORT = 8787;
const DEFAULT_CODE_TTL_MS = 5 * 60 * 1000;
const DEFAULT_SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_ATTEMPTS = 5;
const MAX_BODY_BYTES = 1024 * 1024;

/** Synthetic roster. No real people and no real contact details. */
const DEFAULT_ROSTER = [
  {
    roll_number: 'R1',
    id: 1,
    name: 'Asha Rao',
    role: 'HEAD',
    vertical_ids: [4, 5],
  },
  {
    roll_number: 'R2',
    id: 2,
    name: 'Bilal Khan',
    role: 'VOLUNTEER',
    vertical_ids: [2],
  },
  {
    roll_number: 'R3',
    id: 3,
    name: 'Chen Wei',
    role: 'AH',
    vertical_ids: [1, 2],
  },
  {
    roll_number: 'R4',
    id: 4,
    name: 'Divya Nair',
    role: 'ADMIN',
    vertical_ids: [],
  },
];

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function normalizeFault(value) {
  const raw = String(value ?? 'none')
    .trim()
    .toLowerCase();

  if (raw === '' || raw === 'none') {
    return 'none';
  }
  if (raw === 'wrong-code' || raw === 'wrong_code' || raw === 'wrongcode') {
    return 'wrong-code';
  }
  if (
    raw === 'rate-limit' ||
    raw === 'rate_limit' ||
    raw === 'ratelimit' ||
    raw === '429'
  ) {
    return 'rate-limit';
  }
  if (
    raw === 'unauthorized' ||
    raw === 'unauthorised' ||
    raw === '401'
  ) {
    return 'unauthorized';
  }

  throw new Error(
    `Unknown fault mode "${value}". Use none, wrong-code, rate-limit or unauthorized.`,
  );
}

function requestId() {
  return crypto.randomUUID();
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
    'Cache-Control': 'no-store',
  });
  res.end(payload);
}

function sendEmpty(res, status) {
  res.writeHead(status, { 'Cache-Control': 'no-store' });
  res.end();
}

function sendError(res, status, code) {
  sendJson(res, status, {
    error: {
      code,
      message: 'Request failed.',
      request_id: requestId(),
    },
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;

    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new HttpError(422, 'INVALID_REQUEST', 'Body too large.'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', () =>
      reject(new HttpError(422, 'INVALID_REQUEST', 'Body read failed.')),
    );
  });
}

async function readJsonObject(req) {
  const text = await readBody(req);
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new HttpError(422, 'INVALID_REQUEST', 'Body is not JSON.');
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new HttpError(422, 'INVALID_REQUEST', 'Body must be an object.');
  }
  return parsed;
}

function readRollNumber(body) {
  const value = body.roll_number;
  if (typeof value !== 'string' || value.trim() === '') {
    throw new HttpError(422, 'INVALID_REQUEST', 'roll_number is required.');
  }
  return value.trim();
}

function readCode(body) {
  const value = body.code;
  if (typeof value !== 'string' || !/^\d{6}$/.test(value)) {
    throw new HttpError(422, 'INVALID_REQUEST', 'code must be six digits.');
  }
  return value;
}

function bearerToken(req) {
  const header = req.headers.authorization;
  if (typeof header !== 'string') {
    return null;
  }
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return match ? match[1].trim() : null;
}

function personForResponse(person) {
  return {
    id: person.id,
    name: person.name,
    role: person.role,
    vertical_ids: person.vertical_ids,
  };
}

function sessionPersonForResponse(person) {
  return {
    id: person.id,
    role: person.role,
    vertical_ids: person.vertical_ids,
  };
}

function generateCode() {
  return String(crypto.randomInt(0, 1_000_000)).padStart(6, '0');
}

function generateToken() {
  return `tok_${crypto.randomBytes(16).toString('hex')}`;
}

/**
 * Create the mock server without starting it. Tests inject `log`; the CLI
 * uses console.log.
 */
function createMockServer(options = {}) {
  const fault = normalizeFault(options.fault);
  const log = options.log ?? ((line) => console.log(line));
  const codeTtlMs = options.codeTtlMs ?? DEFAULT_CODE_TTL_MS;
  const sessionTtlMs = options.sessionTtlMs ?? DEFAULT_SESSION_TTL_MS;
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  const rosterEntries = options.roster ?? DEFAULT_ROSTER;

  const roster = new Map(
    rosterEntries.map((person) => [person.roll_number, person]),
  );
  /** @type {Map<string, {code: string, expiresAt: number, attempts: number}>} */
  const pendingCodes = new Map();
  /** @type {Map<string, {person: object, expiresAt: number}>} */
  const sessions = new Map();

  async function handleRequestOtp(req, res) {
    if (fault === 'rate-limit') {
      throw new HttpError(429, 'RATE_LIMITED', 'Too many requests.');
    }

    const body = await readJsonObject(req);
    const rollNumber = readRollNumber(body);
    const person = roster.get(rollNumber);

    pendingCodes.delete(rollNumber);

    if (person) {
      const code = generateCode();
      pendingCodes.set(rollNumber, {
        code,
        expiresAt: Date.now() + codeTtlMs,
        attempts: 0,
      });
      log(`[mock] OTP for ${rollNumber}: ${code}`);
    }

    // Known and unknown roll numbers get exactly the same response.
    sendJson(res, 202, { status: 'accepted' });
  }

  async function handleVerify(req, res) {
    if (fault === 'rate-limit') {
      throw new HttpError(429, 'RATE_LIMITED', 'Too many requests.');
    }

    const body = await readJsonObject(req);
    const rollNumber = readRollNumber(body);
    const code = readCode(body);

    if (fault === 'wrong-code') {
      throw new HttpError(401, 'UNAUTHENTICATED', 'Code not accepted.');
    }

    const person = roster.get(rollNumber);
    const pending = pendingCodes.get(rollNumber);

    if (!person || !pending || pending.expiresAt <= Date.now()) {
      pendingCodes.delete(rollNumber);
      throw new HttpError(401, 'UNAUTHENTICATED', 'Code not accepted.');
    }

    if (pending.attempts >= maxAttempts) {
      pendingCodes.delete(rollNumber);
      throw new HttpError(429, 'RATE_LIMITED', 'Too many attempts.');
    }

    if (pending.code !== code) {
      pending.attempts += 1;
      if (pending.attempts >= maxAttempts) {
        pendingCodes.delete(rollNumber);
        throw new HttpError(429, 'RATE_LIMITED', 'Too many attempts.');
      }
      throw new HttpError(401, 'UNAUTHENTICATED', 'Code not accepted.');
    }

    pendingCodes.delete(rollNumber);

    const token = generateToken();
    const expiresAt = new Date(Date.now() + sessionTtlMs).toISOString();
    sessions.set(token, {
      person,
      expiresAt: Date.parse(expiresAt),
    });

    sendJson(res, 200, {
      token,
      expires_at: expiresAt,
      person: sessionPersonForResponse(person),
    });
  }

  function requireSession(req) {
    if (fault === 'unauthorized') {
      throw new HttpError(401, 'UNAUTHENTICATED', 'Unauthorized.');
    }

    const token = bearerToken(req);
    const session = token ? sessions.get(token) : undefined;

    if (!token || !session) {
      throw new HttpError(401, 'UNAUTHENTICATED', 'Unauthorized.');
    }

    if (session.expiresAt <= Date.now()) {
      sessions.delete(token);
      throw new HttpError(401, 'UNAUTHENTICATED', 'Unauthorized.');
    }

    return { token, session };
  }

  function handleMe(req, res) {
    const { session } = requireSession(req);
    sendJson(res, 200, { person: personForResponse(session.person) });
  }

  function handleLogout(req, res) {
    const { token } = requireSession(req);
    sessions.delete(token);
    sendEmpty(res, 204);
  }

  const server = http.createServer((req, res) => {
    const method = req.method ?? 'GET';
    let pathname = '/';
    try {
      pathname = new URL(req.url ?? '/', 'http://mock.local').pathname;
    } catch {
      sendError(res, 404, 'NOT_FOUND');
      return;
    }

    log(`[mock] ${method} ${pathname}`);

    const route = `${method} ${pathname}`;
    let work;
    if (route === 'POST /auth/otp') {
      work = handleRequestOtp(req, res);
    } else if (route === 'POST /auth/verify') {
      work = handleVerify(req, res);
    } else if (route === 'POST /auth/logout') {
      work = Promise.resolve().then(() => handleLogout(req, res));
    } else if (route === 'GET /me') {
      work = Promise.resolve().then(() => handleMe(req, res));
    } else if (route === 'GET /healthz') {
      sendJson(res, 200, { status: 'ok' });
      return;
    } else {
      sendError(res, 404, 'NOT_FOUND');
      return;
    }

    work.catch((error) => {
      if (res.headersSent) {
        return;
      }
      if (error instanceof HttpError) {
        sendError(res, error.status, error.code);
        return;
      }
      log(`[mock] internal error: ${error?.message ?? 'unknown'}`);
      sendError(res, 500, 'INTERNAL_ERROR');
    });
  });

  return { server, fault };
}

function printUsage() {
  console.log(
    [
      'Usage: node mock-server/server.js [--lan] [--port <n>] [--fault <mode>]',
      '',
      '  --lan            listen on 0.0.0.0 instead of 127.0.0.1',
      '  --port <n>       port to listen on (default 8787, env MOCK_PORT/PORT)',
      '  --fault <mode>   none | wrong-code | rate-limit | unauthorized',
      '                   (default env MOCK_FAULT, otherwise none)',
      '',
      'Codes are printed in this console only; they are never sent to the app.',
    ].join('\n'),
  );
}

function parseArgs(argv) {
  const args = { lan: false, port: undefined, fault: undefined, help: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--lan') {
      args.lan = true;
    } else if (arg === '--help' || arg === '-h') {
      args.help = true;
    } else if (arg === '--port') {
      args.port = argv[index + 1];
      index += 1;
    } else if (arg.startsWith('--port=')) {
      args.port = arg.slice('--port='.length);
    } else if (arg === '--fault') {
      args.fault = argv[index + 1];
      index += 1;
    } else if (arg.startsWith('--fault=')) {
      args.fault = arg.slice('--fault='.length);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`[mock] ${error.message}`);
    printUsage();
    process.exitCode = 1;
    return;
  }

  if (args.help) {
    printUsage();
    return;
  }

  let fault;
  try {
    fault = normalizeFault(args.fault ?? process.env.MOCK_FAULT ?? 'none');
  } catch (error) {
    console.error(`[mock] ${error.message}`);
    process.exitCode = 1;
    return;
  }

  const host = args.lan ? '0.0.0.0' : process.env.MOCK_HOST || DEFAULT_HOST;
  const port = Number(
    args.port ?? process.env.MOCK_PORT ?? process.env.PORT ?? DEFAULT_PORT,
  );
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    console.error('[mock] Port must be an integer between 1 and 65535.');
    process.exitCode = 1;
    return;
  }

  const { server } = createMockServer({ fault, log: (line) => console.log(line) });

  server.listen(port, host, () => {
    console.log(`[mock] ASEC mock API listening on http://${host}:${port}`);
    console.log(`[mock] Fault mode: ${fault}`);
    console.log(
      '[mock] Android emulator: set API_BASE_URL to http://10.0.2.2:' + port,
    );
    if (args.lan || host === '0.0.0.0') {
      console.log(
        '[mock] LAN mode: use http://<this-machine-lan-ip>:' +
          port +
          ' and allow that host in the debug network security config.',
      );
    }
    console.log('[mock] OTP codes appear below. Press Ctrl+C to stop.');
  });

  const shutdown = () => {
    console.log('\n[mock] Stopping.');
    server.close(() => process.exit(0));
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

if (require.main === module) {
  main();
}

module.exports = { createMockServer, normalizeFault };
