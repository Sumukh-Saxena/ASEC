import React from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react-native';
import {
  SessionApi,
  SessionController,
} from '../../src/auth/sessionController';
import { MeResponse, SessionResponse } from '../../src/api/contract';
import { MemorySecureStorage } from '../../src/session/memorySecureStorage';
import { SessionStore } from '../../src/session/sessionStore';
import { SignInFlow, NEUTRAL_CODE_MESSAGE } from '../../src/screens/SignInFlow';
import { HomeScreen } from '../../src/screens/HomeScreen';
import { RestoreErrorScreen } from '../../src/screens/RestoreErrorScreen';

const TOKEN = 'tok_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const CODE = '123456';

const ME: MeResponse = {
  person: {
    id: 7,
    name: 'Test Person',
    role: 'AH',
    vertical_ids: [1, 2],
  },
};

function makeApi(): SessionApi {
  return {
    requestOtp: jest.fn(async () => undefined),
    verifyOtp: jest.fn(
      async (): Promise<SessionResponse> => ({
        token: TOKEN,
        expires_at: '2026-12-03T00:00:00Z',
        person: { id: 7, role: 'AH', vertical_ids: [1, 2] },
      }),
    ),
    me: jest.fn(async () => ME),
    logout: jest.fn(async () => undefined),
  };
}

function Harness({ controller }: { controller: SessionController }) {
  const state = React.useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );

  if (state.status === 'signedIn') {
    return <HomeScreen me={state.me} onSignOut={controller.signOut} />;
  }
  if (state.status === 'restoreError') {
    return (
      <RestoreErrorScreen
        onRetry={controller.restore}
        onSignOut={controller.signOut}
      />
    );
  }
  return (
    <SignInFlow
      requestCode={controller.requestCode}
      verifyCode={controller.verifyCode}
    />
  );
}

describe('console and rendered-text secrecy', () => {
  it('never exposes the token or code during a full sign-in and sign-out cycle', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map(
      (method) => jest.spyOn(console, method).mockImplementation(() => {}),
    );
    const storage = new MemorySecureStorage();
    const store = new SessionStore(storage);
    const controller = new SessionController({ api: makeApi(), store });

    await render(<Harness controller={controller} />);

    await fireEvent.changeText(screen.getByLabelText('Roll number'), 'R1');
    await fireEvent.press(screen.getByText('Send code'));
    await screen.findByText(NEUTRAL_CODE_MESSAGE);

    await fireEvent.changeText(screen.getByLabelText('Six-digit code'), CODE);
    await fireEvent.press(screen.getByText('Verify code'));

    await waitFor(() => expect(screen.getByText('AH')).toBeTruthy());
    expect(await store.load()).toBe(TOKEN);

    const rendered = JSON.stringify(screen.toJSON());
    expect(rendered).not.toContain(TOKEN);
    expect(rendered).not.toContain(CODE);

    const consoleText = spies
      .flatMap((spy) => spy.mock.calls.flat())
      .map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
      .join(' ');
    expect(consoleText).not.toContain(TOKEN);
    expect(consoleText).not.toContain(CODE);

    expect(storage.writes.join(' ')).not.toContain(CODE);

    await fireEvent.press(screen.getByText('Sign out'));
    await waitFor(() => expect(controller.getState()).toEqual({
      status: 'signedOut',
    }));
    expect(await store.load()).toBeNull();

    spies.forEach((spy) => spy.mockRestore());
  });
});
