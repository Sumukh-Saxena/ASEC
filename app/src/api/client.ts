import {
  ENDPOINTS,
  Endpoint,
  IDEMPOTENCY_HEADER,
} from './contract';
import { ApiError, ApiErrorKind } from './errors';

export interface ApiClientOptions {
  baseUrl: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  /** One central hook for any 401; it must clear the local session. */
  onUnauthorized?: () => Promise<void> | void;
}

export interface RequestOptions {
  token?: string | null;
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

const DEFAULT_TIMEOUT_MS = 10_000;

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, '');
}

function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) {
    return 'unauthorized';
  }
  if (status === 403) {
    return 'forbidden';
  }
  if (status === 404) {
    return 'not_found';
  }
  if (status === 409) {
    return 'conflict';
  }
  if (status === 422) {
    return 'validation';
  }
  if (status === 429) {
    return 'rate_limited';
  }
  if (status >= 500) {
    return 'server';
  }
  return 'unexpected_status';
}

function isRetryable(error: ApiError): boolean {
  return (
    error.kind === 'network' ||
    error.kind === 'timeout' ||
    error.kind === 'server'
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Read only the server code; the message and raw body are discarded. */
function readErrorCode(text: string): string | undefined {
  try {
    const parsed: unknown = JSON.parse(text);
    if (!isRecord(parsed) || !isRecord(parsed.error)) {
      return undefined;
    }
    const code = parsed.error.code;
    return typeof code === 'string' ? code : undefined;
  } catch {
    return undefined;
  }
}

function readJson(text: string): unknown {
  if (text.trim() === '') {
    throw new ApiError('unexpected_body');
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiError('unexpected_body');
  }
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly onUnauthorized?: () => Promise<void> | void;

  constructor(options: ApiClientOptions) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.onUnauthorized = options.onUnauthorized;
  }

  get<T>(endpoint: Endpoint, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(endpoint, options);
  }

  post<T>(endpoint: Endpoint, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(endpoint, options);
  }

  async request<T>(
    endpoint: Endpoint,
    options: RequestOptions = {},
  ): Promise<T> {
    const attempts = endpoint.method === 'GET' ? 2 : 1;
    let lastError: ApiError | undefined;

    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        return await this.execute<T>(endpoint, options);
      } catch (error) {
        if (!(error instanceof ApiError)) {
          throw error;
        }
        lastError = error;
        const hasAnotherAttempt = attempt + 1 < attempts;
        if (hasAnotherAttempt && isRetryable(error)) {
          continue;
        }
        throw error;
      }
    }

    throw lastError ?? new ApiError('network');
  }

  private async execute<T>(
    endpoint: Endpoint,
    options: RequestOptions,
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const forwardAbort = () => controller.abort();

    if (options.signal) {
      if (options.signal.aborted) {
        controller.abort();
      } else {
        options.signal.addEventListener('abort', forwardAbort, { once: true });
      }
    }

    const headers: Record<string, string> = {
      Accept: 'application/json',
    };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (options.token) {
      headers.Authorization = `Bearer ${options.token}`;
    }
    if (options.idempotencyKey) {
      headers[IDEMPOTENCY_HEADER] = options.idempotencyKey;
    }

    const init: RequestInit = {
      method: endpoint.method,
      headers,
      credentials: 'omit',
      signal: controller.signal,
    };
    if (options.body !== undefined) {
      init.body = JSON.stringify(options.body);
    }

    try {
      let response: Response;
      try {
        response = await this.fetchImpl(
          `${this.baseUrl}${endpoint.path}`,
          init,
        );
      } catch {
        throw new ApiError(controller.signal.aborted ? 'timeout' : 'network');
      }

      let text: string;
      try {
        text = await response.text();
      } catch {
        throw new ApiError('unexpected_body');
      }

      if (response.status === 401) {
        await this.notifyUnauthorized();
        throw new ApiError('unauthorized', 401, readErrorCode(text));
      }

      if (response.status < 200 || response.status >= 300) {
        throw new ApiError(
          kindForStatus(response.status),
          response.status,
          readErrorCode(text),
        );
      }

      if (response.status === 204) {
        return undefined as T;
      }

      const parsed = readJson(text);
      if (!isRecord(parsed)) {
        throw new ApiError('unexpected_body');
      }
      return parsed as T;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener('abort', forwardAbort);
    }
  }

  private async notifyUnauthorized(): Promise<void> {
    if (!this.onUnauthorized) {
      return;
    }
    try {
      await this.onUnauthorized();
    } catch {
      // Clearing the session must not mask the 401 itself.
    }
  }
}

/**
 * A fresh idempotency key for one user action. Reuse the same value when
 * retrying that action; the client never retries a POST on its own.
 */
export function newIdempotencyKey(): string {
  const bytes = new Uint8Array(16);
  const cryptoObject = (
    globalThis as {
      crypto?: { getRandomValues?: (array: Uint8Array) => Uint8Array };
    }
  ).crypto;

  if (cryptoObject?.getRandomValues) {
    cryptoObject.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }

  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(
    '',
  );
}

/** Convenience for callers that need the five session endpoints together. */
export const SESSION_ENDPOINTS = {
  requestOtp: ENDPOINTS.requestOtp,
  verifyOtp: ENDPOINTS.verifyOtp,
  logout: ENDPOINTS.logout,
  me: ENDPOINTS.me,
  healthz: ENDPOINTS.healthz,
} as const;
