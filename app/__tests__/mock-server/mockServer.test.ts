import { createMockServer } from '../../mock-server/server';

const TOKEN_PATTERN = /^tok_[0-9a-f]{32}$/;
const CODE_PATTERN = /^\d{6}$/;

interface RunningServer {
  baseUrl: string;
  logs: string[];
  close: () => Promise<void>;
}

async function startServer(fault = 'none'): Promise<RunningServer> {
  const logs: string[] = [];
  const { server } = createMockServer({
    fault,
    log: (line: string) => logs.push(line),
  });

  await new Promise<void>((resolve) => {
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (typeof address !== 'object' || address === null) {
    throw new Error('Mock server did not expose a TCP address');
  }
  const { port } = address;

  return {
    baseUrl: `http://127.0.0.1:${port}`,
    logs,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error?: Error | null) =>
          error ? reject(error) : resolve(),
        );
      }),
  };
}

function codeFromLogs(logs: string[], rollNumber: string): string {
  const line = logs.find((entry) =>
    entry.includes(`OTP for ${rollNumber}:`),
  );
  if (!line) {
    throw new Error(`No OTP log line for ${rollNumber}`);
  }
  const code = line.split(':').pop()?.trim() ?? '';
  if (!CODE_PATTERN.test(code)) {
    throw new Error(`Malformed OTP log line: ${line}`);
  }
  return code;
}

async function request(
  baseUrl: string,
  path: string,
  options: {
    method?: string;
    body?: unknown;
    token?: string;
    rawBody?: string;
  } = {},
): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined || options.rawBody !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }

  return fetch(`${baseUrl}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body:
      options.rawBody ??
      (options.body === undefined ? undefined : JSON.stringify(options.body)),
  });
}

describe('mock server', () => {
  let running: RunningServer;

  afterEach(async () => {
    await running.close();
  });

  it('answers GET /healthz without authentication', async () => {
    running = await startServer();

    const response = await request(running.baseUrl, '/healthz');

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ status: 'ok' });
  });

  it('gives known and unknown roll numbers the same neutral OTP response', async () => {
    running = await startServer();

    const known = await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R1' },
    });
    const unknown = await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'NOT-IN-ROSTER' },
    });

    expect(known.status).toBe(202);
    expect(unknown.status).toBe(202);
    await expect(known.json()).resolves.toEqual({ status: 'accepted' });
    await expect(unknown.json()).resolves.toEqual({ status: 'accepted' });

    expect(codeFromLogs(running.logs, 'R1')).toMatch(CODE_PATTERN);
    expect(
      running.logs.some((line) => line.includes('NOT-IN-ROSTER')),
    ).toBe(false);
  });

  it('verifies a logged code, serves GET /me, then invalidates the token on logout', async () => {
    running = await startServer();

    await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R1' },
    });
    const code = codeFromLogs(running.logs, 'R1');

    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R1', code },
    });
    expect(verify.status).toBe(200);
    const session = await verify.json();
    expect(session.token).toMatch(TOKEN_PATTERN);
    expect(session.expires_at).toEqual(expect.any(String));
    expect(session.person).toMatchObject({
      id: 1,
      role: 'HEAD',
      vertical_ids: [4, 5],
    });
    expect(JSON.stringify(session)).not.toContain(code);

    const me = await request(running.baseUrl, '/me', {
      token: session.token,
    });
    expect(me.status).toBe(200);
    const meBody = await me.json();
    expect(meBody.person).toMatchObject({ id: 1, name: expect.any(String) });

    const logout = await request(running.baseUrl, '/auth/logout', {
      method: 'POST',
      token: session.token,
    });
    expect(logout.status).toBe(204);
    await expect(logout.text()).resolves.toBe('');

    const afterLogout = await request(running.baseUrl, '/me', {
      token: session.token,
    });
    expect(afterLogout.status).toBe(401);
    await expect(afterLogout.json()).resolves.toMatchObject({
      error: { code: 'UNAUTHENTICATED' },
    });

    expect(running.logs.join('\n')).not.toContain(session.token);
  });

  it('rejects a wrong code with 401 and never returns a token', async () => {
    running = await startServer();

    await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R1' },
    });
    const code = codeFromLogs(running.logs, 'R1');
    const wrong = code === '000000' ? '111111' : '000000';

    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R1', code: wrong },
    });

    expect(verify.status).toBe(401);
    const body = await verify.json();
    expect(body).toMatchObject({ error: { code: 'UNAUTHENTICATED' } });
    expect(JSON.stringify(body)).not.toContain('token');
  });

  it('rejects a correct code in wrong-code fault mode', async () => {
    running = await startServer('wrong-code');

    await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R1' },
    });
    const code = codeFromLogs(running.logs, 'R1');

    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R1', code },
    });

    expect(verify.status).toBe(401);
    await expect(verify.json()).resolves.toMatchObject({
      error: { code: 'UNAUTHENTICATED' },
    });
  });

  it('returns 429 for OTP and verify in rate-limit fault mode', async () => {
    running = await startServer('rate-limit');

    const otp = await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R1' },
    });
    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R1', code: '123456' },
    });

    expect(otp.status).toBe(429);
    expect(verify.status).toBe(429);
    await expect(otp.json()).resolves.toMatchObject({
      error: { code: 'RATE_LIMITED' },
    });
    await expect(verify.json()).resolves.toMatchObject({
      error: { code: 'RATE_LIMITED' },
    });
  });

  it('returns 401 from authenticated routes in unauthorized fault mode', async () => {
    running = await startServer('unauthorized');

    await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'R2' },
    });
    const code = codeFromLogs(running.logs, 'R2');
    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R2', code },
    });
    const session = await verify.json();

    const me = await request(running.baseUrl, '/me', {
      token: session.token,
    });
    const logout = await request(running.baseUrl, '/auth/logout', {
      method: 'POST',
      token: session.token,
    });

    expect(me.status).toBe(401);
    expect(logout.status).toBe(401);
    await expect(me.json()).resolves.toMatchObject({
      error: { code: 'UNAUTHENTICATED' },
    });
  });

  it('rejects malformed input and unknown routes with uniform error bodies', async () => {
    running = await startServer();

    const missingRoll = await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: {},
    });
    const shortCode = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'R1', code: '12345' },
    });
    const badJson = await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      rawBody: '{not json',
    });
    const unknownRoute = await request(running.baseUrl, '/nope');

    expect(missingRoll.status).toBe(422);
    expect(shortCode.status).toBe(422);
    expect(badJson.status).toBe(422);
    expect(unknownRoute.status).toBe(404);

    await expect(missingRoll.json()).resolves.toMatchObject({
      error: { code: 'INVALID_REQUEST', request_id: expect.any(String) },
    });
    await expect(unknownRoute.json()).resolves.toMatchObject({
      error: { code: 'NOT_FOUND' },
    });
  });

  it('rejects an unknown roll number even with a correctly shaped code', async () => {
    running = await startServer();

    await request(running.baseUrl, '/auth/otp', {
      method: 'POST',
      body: { roll_number: 'NOT-IN-ROSTER' },
    });
    const verify = await request(running.baseUrl, '/auth/verify', {
      method: 'POST',
      body: { roll_number: 'NOT-IN-ROSTER', code: '123456' },
    });

    expect(verify.status).toBe(401);
    await expect(verify.json()).resolves.toMatchObject({
      error: { code: 'UNAUTHENTICATED' },
    });
  });
});
