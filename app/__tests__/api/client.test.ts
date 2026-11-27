import { ApiClient, newIdempotencyKey } from '../../src/api/client';
import { ENDPOINTS } from '../../src/api/contract';
import { ApiError } from '../../src/api/errors';

function fakeResponse(status: number, body?: unknown): Response {
  const text =
    body === undefined
      ? ''
      : typeof body === 'string'
        ? body
        : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  } as unknown as Response;
}

function errorBody(code: string, message = 'server text') {
  return { error: { code, message, request_id: 'req-1' } };
}

function makeClient(
  fetchImpl: jest.Mock,
  onUnauthorized?: () => Promise<void> | void,
  timeoutMs = 1000,
) {
  return new ApiClient({
    baseUrl: 'https://api.invalid.example/',
    fetchImpl: fetchImpl as unknown as typeof fetch,
    onUnauthorized,
    timeoutMs,
  });
}

describe('ApiClient request construction', () => {
  it('joins the base URL and sends the Bearer header without cookies', async () => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(200, { status: 'ok' }));
    const client = makeClient(fetchImpl);

    await expect(client.get(ENDPOINTS.healthz)).resolves.toEqual({
      status: 'ok',
    });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('https://api.invalid.example/healthz');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('omit');
    expect(init.headers.Authorization).toBeUndefined();

    fetchImpl.mockResolvedValueOnce(
      fakeResponse(200, {
        person: { id: 1, name: 'A', role: 'HEAD', vertical_ids: [2] },
      }),
    );
    await client.get(ENDPOINTS.me, { token: 'tok_example' });
    const [, authedInit] = fetchImpl.mock.calls[1];
    expect(authedInit.headers.Authorization).toBe('Bearer tok_example');
  });

  it('sends an optional Idempotency-Key and a JSON body', async () => {
    const fetchImpl = jest.fn().mockResolvedValue(fakeResponse(204));
    const client = makeClient(fetchImpl);

    await client.post(ENDPOINTS.logout, {
      token: 'tok_example',
      idempotencyKey: '0123456789abcdef0123456789abcdef',
    });

    const [, init] = fetchImpl.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.headers['Idempotency-Key']).toBe(
      '0123456789abcdef0123456789abcdef',
    );
    expect(init.body).toBeUndefined();
  });

  it('creates fresh idempotency keys in the server accepted charset', () => {
    const first = newIdempotencyKey();
    const second = newIdempotencyKey();
    expect(first).toMatch(/^[A-Za-z0-9._:-]{16,64}$/);
    expect(second).toMatch(/^[A-Za-z0-9._:-]{16,64}$/);
    expect(first).not.toBe(second);
  });
});

describe('ApiClient response handling', () => {
  it('treats 204 as a successful empty response', async () => {
    const fetchImpl = jest.fn().mockResolvedValue(fakeResponse(204));
    const client = makeClient(fetchImpl);

    await expect(
      client.post(ENDPOINTS.logout, { token: 'tok_example' }),
    ).resolves.toBeUndefined();
  });

  it('rejects an unexpected success body without echoing it', async () => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(200, 'not-json SECRET_TOKEN 123456'));
    const client = makeClient(fetchImpl);

    const error = await client.get(ENDPOINTS.me).catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ kind: 'unexpected_body' });
    expect(String(error)).not.toContain('SECRET_TOKEN');
    expect(String(error)).not.toContain('123456');
  });

  it.each([
    [403, 'FORBIDDEN', 'forbidden'],
    [404, 'NOT_FOUND', 'not_found'],
    [409, 'CONFLICT', 'conflict'],
    [422, 'INVALID_REQUEST', 'validation'],
    [429, 'RATE_LIMITED', 'rate_limited'],
    [500, 'INTERNAL_ERROR', 'server'],
    [503, 'INTERNAL_ERROR', 'server'],
  ])('maps HTTP %i to %s', async (status, code, kind) => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(status, errorBody(code)));
    const client = makeClient(fetchImpl);

    const error = await client.get(ENDPOINTS.me).catch((caught) => caught);

    expect(error).toMatchObject({ kind, status });
    if (status < 500) {
      expect(error).toMatchObject({ code });
    }
  });

  it('never puts the response body, token or code in the error', async () => {
    const fetchImpl = jest.fn().mockResolvedValue(
      fakeResponse(
        401,
        errorBody('UNAUTHENTICATED', 'SECRET_TOKEN 123456 raw body'),
      ),
    );
    const client = makeClient(fetchImpl);

    const error = await client.get(ENDPOINTS.me, { token: 'SECRET_TOKEN' }).catch(
      (caught) => caught,
    );

    expect(error).toMatchObject({ kind: 'unauthorized', status: 401 });
    expect(String(error)).not.toContain('SECRET_TOKEN');
    expect(String(error)).not.toContain('123456');
    expect(JSON.stringify(error)).not.toContain('SECRET_TOKEN');
    expect(JSON.stringify(error)).not.toContain('123456');
  });
});

describe('ApiClient failure mapping and retry policy', () => {
  it('maps a rejected fetch to network without echoing the cause', async () => {
    const fetchImpl = jest
      .fn()
      .mockRejectedValue(new Error('socket SECRET_TOKEN 123456'));
    const client = makeClient(fetchImpl);

    const error = await client.post(ENDPOINTS.requestOtp, {
      body: { roll_number: 'R1' },
    }).catch((caught) => caught);

    expect(error).toMatchObject({ kind: 'network' });
    expect(String(error)).not.toContain('SECRET_TOKEN');
    expect(String(error)).not.toContain('123456');
  });

  it('maps an aborted request to timeout', async () => {
    const fetchImpl = jest.fn(
      (_url: string, init: { signal: AbortSignal }) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            reject(new Error('aborted'));
          });
        }),
    );
    const client = makeClient(fetchImpl, undefined, 5);

    await expect(client.get(ENDPOINTS.healthz)).rejects.toMatchObject({
      kind: 'timeout',
    });
  });

  it('retries an idempotent GET exactly once after a network failure', async () => {
    const fetchImpl = jest
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(fakeResponse(200, { status: 'ok' }));
    const client = makeClient(fetchImpl);

    await expect(client.get(ENDPOINTS.healthz)).resolves.toEqual({
      status: 'ok',
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it('retries an idempotent GET once after a 5xx response', async () => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValueOnce(fakeResponse(503, errorBody('INTERNAL_ERROR')))
      .mockResolvedValueOnce(fakeResponse(200, { status: 'ok' }));
    const client = makeClient(fetchImpl);

    await expect(client.get(ENDPOINTS.healthz)).resolves.toEqual({
      status: 'ok',
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it('never retries a POST', async () => {
    const fetchImpl = jest.fn().mockRejectedValue(new Error('offline'));
    const client = makeClient(fetchImpl);

    await expect(
      client.post(ENDPOINTS.requestOtp, { body: { roll_number: 'R1' } }),
    ).rejects.toMatchObject({ kind: 'network' });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('does not retry a GET after a 401', async () => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(401, errorBody('UNAUTHENTICATED')));
    const client = makeClient(fetchImpl);

    await expect(client.get(ENDPOINTS.me)).rejects.toMatchObject({
      kind: 'unauthorized',
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe('ApiClient central 401 handler', () => {
  it('invokes the central handler exactly once for a 401', async () => {
    const onUnauthorized = jest.fn(async () => {});
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(401, errorBody('UNAUTHENTICATED')));
    const client = makeClient(fetchImpl, onUnauthorized);

    await expect(client.get(ENDPOINTS.me)).rejects.toMatchObject({
      kind: 'unauthorized',
    });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('does not invoke the central handler for other failures', async () => {
    const onUnauthorized = jest.fn(async () => {});
    const fetchImpl = jest
      .fn()
      .mockResolvedValue(fakeResponse(403, errorBody('FORBIDDEN')));
    const client = makeClient(fetchImpl, onUnauthorized);

    await expect(client.get(ENDPOINTS.me)).rejects.toMatchObject({
      kind: 'forbidden',
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
