import { SecureStorage } from './secureStorage';

const MIN_TOKEN_LENGTH = 16;
const MAX_TOKEN_LENGTH = 512;

function isUsableToken(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length >= MIN_TOKEN_LENGTH &&
    value.length <= MAX_TOKEN_LENGTH &&
    !/\s/.test(value)
  );
}

/**
 * Saves and loads the session token through a SecureStorage seam.
 *
 * A corrupt, unreadable or malformed stored value is cleared and reported as
 * signed out. The token is never logged and never returned in an error.
 */
export class SessionStore {
  constructor(private readonly storage: SecureStorage) {}

  async save(token: string): Promise<void> {
    if (!isUsableToken(token)) {
      throw new Error('invalid session token');
    }
    await this.storage.write(JSON.stringify({ token }));
  }

  async load(): Promise<string | null> {
    let raw: string | null;
    try {
      raw = await this.storage.read();
    } catch {
      await this.clearQuietly();
      return null;
    }

    if (raw === null || raw === '') {
      return null;
    }

    try {
      const parsed: unknown = JSON.parse(raw);
      if (
        typeof parsed === 'object' &&
        parsed !== null &&
        isUsableToken((parsed as { token?: unknown }).token)
      ) {
        return (parsed as { token: string }).token;
      }
    } catch {
      // Fall through to clear the corrupt value.
    }

    await this.clearQuietly();
    return null;
  }

  async clear(): Promise<void> {
    await this.storage.clear();
  }

  private async clearQuietly(): Promise<void> {
    try {
      await this.storage.clear();
    } catch {
      // A storage that cannot clear still reports signed out.
    }
  }
}
