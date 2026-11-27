import { SecureStorage } from './secureStorage';

/** Test double; never used by the app. */
export class MemorySecureStorage implements SecureStorage {
  value: string | null = null;
  writes: string[] = [];
  failRead = false;
  failWrite = false;
  failClear = false;

  async read(): Promise<string | null> {
    if (this.failRead) {
      throw new Error('unreadable secure storage');
    }
    return this.value;
  }

  async write(value: string): Promise<void> {
    if (this.failWrite) {
      throw new Error('unwritable secure storage');
    }
    this.writes.push(value);
    this.value = value;
  }

  async clear(): Promise<void> {
    if (this.failClear) {
      throw new Error('unclearable secure storage');
    }
    this.value = null;
  }
}
