import { MemorySecureStorage } from '../../src/session/memorySecureStorage';
import { SessionStore } from '../../src/session/sessionStore';

const TOKEN = 'tok_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

describe('SessionStore', () => {
  it('saves, loads and clears a session through secure storage', async () => {
    const storage = new MemorySecureStorage();
    const store = new SessionStore(storage);

    await store.save(TOKEN);
    await expect(store.load()).resolves.toBe(TOKEN);
    expect(storage.writes).toHaveLength(1);
    expect(storage.writes[0]).not.toContain('123456');

    await store.clear();
    await expect(store.load()).resolves.toBeNull();
  });

  it('clears a corrupt JSON value and returns signed out', async () => {
    const storage = new MemorySecureStorage();
    storage.value = '{not-json';
    const store = new SessionStore(storage);

    await expect(store.load()).resolves.toBeNull();
    expect(storage.value).toBeNull();
  });

  it('clears a well-formed value with an invalid token and returns signed out', async () => {
    const storage = new MemorySecureStorage();
    storage.value = JSON.stringify({ token: '' });
    const store = new SessionStore(storage);

    await expect(store.load()).resolves.toBeNull();
    expect(storage.value).toBeNull();
  });

  it('clears an unreadable value and returns signed out', async () => {
    const storage = new MemorySecureStorage();
    storage.value = JSON.stringify({ token: TOKEN });
    storage.failRead = true;
    const store = new SessionStore(storage);

    await expect(store.load()).resolves.toBeNull();
    expect(storage.value).toBeNull();
  });

  it('still returns signed out when the corrupt value cannot be cleared', async () => {
    const storage = new MemorySecureStorage();
    storage.value = JSON.stringify({ token: TOKEN });
    storage.failRead = true;
    storage.failClear = true;
    const store = new SessionStore(storage);

    await expect(store.load()).resolves.toBeNull();
  });

  it('refuses to save an empty or malformed token', async () => {
    const storage = new MemorySecureStorage();
    const store = new SessionStore(storage);

    await expect(store.save('')).rejects.toThrow('invalid session token');
    await expect(store.save('has whitespace inside')).rejects.toThrow(
      'invalid session token',
    );
    expect(storage.writes).toHaveLength(0);
  });
});
