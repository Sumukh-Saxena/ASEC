import { ApiError } from '../../src/api/errors';
import {
  SessionApi,
  SessionController,
} from '../../src/auth/sessionController';
import { MeResponse, SessionResponse } from '../../src/api/contract';
import { MemorySecureStorage } from '../../src/session/memorySecureStorage';
import { SessionStore } from '../../src/session/sessionStore';

const TOKEN = 'tok_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

const ME: MeResponse = {
  person: {
    id: 7,
    name: 'Test Person',
    role: 'VOLUNTEER',
    vertical_ids: [2, 5],
  },
};

function makeApi(overrides: Partial<SessionApi> = {}): SessionApi {
  return {
    requestOtp: jest.fn(async () => undefined),
    verifyOtp: jest.fn(
      async (): Promise<SessionResponse> => ({
        token: TOKEN,
        expires_at: '2026-12-03T00:00:00Z',
        person: {
          id: 7,
          role: 'WRONG_ROLE_FROM_VERIFY',
          vertical_ids: [99],
        },
      }),
    ),
    me: jest.fn(async () => ME),
    logout: jest.fn(async () => undefined),
    ...overrides,
  };
}

describe('SessionController restore', () => {
  it('starts signed out when no token is stored', async () => {
    const api = makeApi();
    const store = new SessionStore(new MemorySecureStorage());
    const controller = new SessionController({ api, store });

    await controller.restore();

    expect(controller.getState()).toEqual({ status: 'signedOut' });
    expect(api.me).not.toHaveBeenCalled();
  });

  it('restores a signed-in session from GET /me', async () => {
    const api = makeApi();
    const store = new SessionStore(new MemorySecureStorage());
    await store.save(TOKEN);
    const controller = new SessionController({ api, store });

    await controller.restore();

    expect(api.me).toHaveBeenCalledWith(TOKEN);
    expect(controller.getState()).toEqual({
      status: 'signedIn',
      me: ME.person,
    });
    expect(JSON.stringify(controller.getState())).not.toContain(TOKEN);
  });

  it('clears the stored token and signs out on a 401', async () => {
    const api = makeApi({
      me: jest.fn(async () => {
        throw new ApiError('unauthorized', 401);
      }),
    });
    const store = new SessionStore(new MemorySecureStorage());
    await store.save(TOKEN);
    const controller = new SessionController({ api, store });

    await controller.restore();

    expect(controller.getState()).toEqual({ status: 'signedOut' });
    await expect(store.load()).resolves.toBeNull();
  });

  it('shows a retry state on a network error and keeps the token', async () => {
    const api = makeApi({
      me: jest.fn(async () => {
        throw new ApiError('network');
      }),
    });
    const store = new SessionStore(new MemorySecureStorage());
    await store.save(TOKEN);
    const controller = new SessionController({ api, store });

    await controller.restore();

    expect(controller.getState()).toEqual({ status: 'restoreError' });
    await expect(store.load()).resolves.toBe(TOKEN);
  });
});

describe('SessionController sign-in', () => {
  it('saves the token and reads role and verticals only from GET /me', async () => {
    const api = makeApi();
    const store = new SessionStore(new MemorySecureStorage());
    const controller = new SessionController({ api, store });

    await controller.requestCode('R1');
    expect(api.requestOtp).toHaveBeenCalledWith('R1');

    await controller.verifyCode('R1', '123456');

    expect(api.verifyOtp).toHaveBeenCalledWith('R1', '123456');
    expect(api.me).toHaveBeenCalledWith(TOKEN);
    await expect(store.load()).resolves.toBe(TOKEN);
    expect(controller.getState()).toEqual({
      status: 'signedIn',
      me: ME.person,
    });
  });

  it('does not save a token when the code is rejected', async () => {
    const api = makeApi({
      verifyOtp: jest.fn(async () => {
        throw new ApiError('unauthorized', 401);
      }),
    });
    const store = new SessionStore(new MemorySecureStorage());
    const controller = new SessionController({ api, store });

    await expect(controller.verifyCode('R1', '123456')).rejects.toMatchObject({
      kind: 'unauthorized',
    });

    await expect(store.load()).resolves.toBeNull();
    expect(controller.getState()).toEqual({ status: 'signedOut' });
  });
});

describe('SessionController sign-out and central 401', () => {
  it('calls POST /auth/logout and clears locally even when logout fails', async () => {
    const api = makeApi({
      logout: jest.fn(async () => {
        throw new ApiError('network');
      }),
    });
    const store = new SessionStore(new MemorySecureStorage());
    await store.save(TOKEN);
    const controller = new SessionController({ api, store });

    await controller.signOut();

    expect(api.logout).toHaveBeenCalledWith(TOKEN);
    await expect(store.load()).resolves.toBeNull();
    expect(controller.getState()).toEqual({ status: 'signedOut' });
  });

  it('clears the session when the client reports a central 401', async () => {
    const api = makeApi();
    const store = new SessionStore(new MemorySecureStorage());
    await store.save(TOKEN);
    const controller = new SessionController({ api, store });

    await controller.handleUnauthorized();

    await expect(store.load()).resolves.toBeNull();
    expect(controller.getState()).toEqual({ status: 'signedOut' });
  });
});
