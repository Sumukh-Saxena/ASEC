/**
 * Small typed API failures.
 *
 * The error message is the kind only. It never contains a response body, a
 * request body, a session token, an OTP code, a URL or a server message.
 */
export type ApiErrorKind =
  | 'network'
  | 'timeout'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'validation'
  | 'rate_limited'
  | 'server'
  | 'unexpected_body'
  | 'unexpected_status';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  /** Server error code from ErrorResponse, never an OTP code. */
  readonly code?: string;

  constructor(kind: ApiErrorKind, status?: number, code?: string) {
    super(kind);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}
