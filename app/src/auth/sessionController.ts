import { MeResponse, Person, SessionResponse } from '../api/contract';
import { ApiError } from '../api/errors';
import { SessionStore } from '../session/sessionStore';

export interface SessionApi {
  requestOtp(rollNumber: string): Promise<void>;
  verifyOtp(rollNumber: string, code: string): Promise<SessionResponse>;
  me(token: string): Promise<MeResponse>;
  logout(token: string): Promise<void>;
}

export type SessionState =
  | { status: 'loading' }
  | { status: 'signedOut' }
  | { status: 'signedIn'; me: Person }
  | { status: 'restoreError' };

export interface SessionControllerOptions {
  api: SessionApi;
  store: SessionStore;
}

/**
 * Owns the session state and the only place a token is saved or cleared.
 *
 * The token never enters SessionState, navigation params, rendered text or an
 * error message. Role and verticals are taken only from GET /me.
 */
export class SessionController {
  private readonly api: SessionApi;
  private readonly store: SessionStore;
  private state: SessionState = { status: 'loading' };
  private readonly listeners = new Set<() => void>();
  private restoring = false;

  constructor(options: SessionControllerOptions) {
    this.api = options.api;
    this.store = options.store;
  }

  getState = (): SessionState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  restore = async (): Promise<void> => {
    if (this.restoring) {
      return;
    }
    this.restoring = true;
    try {
      const token = await this.store.load();
      if (!token) {
        this.setState({ status: 'signedOut' });
        return;
      }

      try {
        const me = await this.api.me(token);
        this.setState({ status: 'signedIn', me: me.person });
      } catch (error) {
        if (error instanceof ApiError && error.kind === 'unauthorized') {
          await this.clearQuietly();
          this.setState({ status: 'signedOut' });
          return;
        }
        this.setState({ status: 'restoreError' });
      }
    } finally {
      this.restoring = false;
    }
  };

  requestCode = async (rollNumber: string): Promise<void> => {
    await this.api.requestOtp(rollNumber);
  };

  verifyCode = async (rollNumber: string, code: string): Promise<void> => {
    let session: SessionResponse;
    try {
      session = await this.api.verifyOtp(rollNumber, code);
    } catch (error) {
      if (error instanceof ApiError && error.kind === 'unauthorized') {
        // A rejected code never creates a session; keep the sign-in screen.
        this.setState({ status: 'signedOut' });
      }
      throw error;
    }

    await this.store.save(session.token);

    try {
      const me = await this.api.me(session.token);
      this.setState({ status: 'signedIn', me: me.person });
    } catch (error) {
      if (error instanceof ApiError && error.kind === 'unauthorized') {
        await this.clearQuietly();
        this.setState({ status: 'signedOut' });
      } else {
        this.setState({ status: 'restoreError' });
      }
      throw error;
    }
  };

  signOut = async (): Promise<void> => {
    let token: string | null = null;
    try {
      token = await this.store.load();
    } catch {
      token = null;
    }

    if (token) {
      try {
        await this.api.logout(token);
      } catch {
        // Local sign-out must succeed even when the server call fails.
      }
    }

    await this.clearQuietly();
    this.setState({ status: 'signedOut' });
  };

  /** The client's single central 401 hook. */
  handleUnauthorized = async (): Promise<void> => {
    await this.clearQuietly();
    this.setState({ status: 'signedOut' });
  };

  private setState(next: SessionState): void {
    this.state = next;
    this.listeners.forEach((listener) => listener());
  }

  private async clearQuietly(): Promise<void> {
    try {
      await this.store.clear();
    } catch {
      // An unreadable storage still leaves the UI signed out.
    }
  }
}
