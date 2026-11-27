import * as Keychain from 'react-native-keychain';
import { SecureStorage } from './secureStorage';

const SERVICE = 'com.asec.app.session';

/**
 * Android Keystore-backed storage. The token is the only value written here;
 * it is never logged and never placed in navigation state.
 */
export class KeychainSecureStorage implements SecureStorage {
  async read(): Promise<string | null> {
    const credentials = await Keychain.getGenericPassword({ service: SERVICE });
    return credentials ? credentials.password : null;
  }

  async write(value: string): Promise<void> {
    const result = await Keychain.setGenericPassword('session', value, {
      service: SERVICE,
    });
    if (!result) {
      throw new Error('secure storage unavailable');
    }
  }

  async clear(): Promise<void> {
    await Keychain.resetGenericPassword({ service: SERVICE });
  }
}
