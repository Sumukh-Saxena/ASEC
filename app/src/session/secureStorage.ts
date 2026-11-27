/**
 * The small secure-storage seam. Production uses the Android Keystore through
 * react-native-keychain; tests use an in-memory fake.
 */
export interface SecureStorage {
  read(): Promise<string | null>;
  write(value: string): Promise<void>;
  clear(): Promise<void>;
}
