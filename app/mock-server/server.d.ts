/**
 * Minimal structural type for the Node HTTP server used by the mock.
 *
 * The app's TypeScript config does not include Node's global types, so this
 * declaration deliberately avoids importing from `node:http`.
 */
export interface MockHttpServer {
  listen(port: number, host: string, callback?: () => void): void;
  address(): { port: number } | string | null;
  close(callback?: (error?: Error | null) => void): void;
}

export type MockFault =
  | 'none'
  | 'wrong-code'
  | 'rate-limit'
  | 'unauthorized';

export interface MockRosterEntry {
  roll_number: string;
  id: number;
  name: string;
  role: string;
  vertical_ids: number[];
}

export interface MockServerOptions {
  fault?: MockFault | string;
  log?: (line: string) => void;
  codeTtlMs?: number;
  sessionTtlMs?: number;
  maxAttempts?: number;
  roster?: MockRosterEntry[];
}

export interface MockServer {
  server: MockHttpServer;
  fault: MockFault;
}

export function createMockServer(options?: MockServerOptions): MockServer;
export function normalizeFault(value: string): MockFault;
