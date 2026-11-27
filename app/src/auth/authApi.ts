import type { SessionApi } from './sessionController';
import { ApiClient } from '../api/client';
import {
  ENDPOINTS,
  HealthResponse,
  MeResponse,
  OtpResponse,
  SessionResponse,
} from '../api/contract';
import { ApiError } from '../api/errors';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSessionResponse(value: unknown): value is SessionResponse {
  if (!isRecord(value) || typeof value.token !== 'string') {
    return false;
  }
  if (typeof value.expires_at !== 'string' || !isRecord(value.person)) {
    return false;
  }
  return (
    typeof value.person.id === 'number' &&
    typeof value.person.role === 'string' &&
    Array.isArray(value.person.vertical_ids)
  );
}

function isMeResponse(value: unknown): value is MeResponse {
  if (!isRecord(value) || !isRecord(value.person)) {
    return false;
  }
  return (
    typeof value.person.id === 'number' &&
    typeof value.person.name === 'string' &&
    typeof value.person.role === 'string' &&
    Array.isArray(value.person.vertical_ids)
  );
}

function isOtpResponse(value: unknown): value is OtpResponse {
  return isRecord(value) && value.status === 'accepted';
}

function isHealthResponse(value: unknown): value is HealthResponse {
  return isRecord(value) && typeof value.status === 'string';
}

/** The real HTTP implementation of the small session API used by the app. */
export class HttpAuthApi implements SessionApi {
  constructor(private readonly client: ApiClient) {}

  async requestOtp(rollNumber: string): Promise<void> {
    const response = await this.client.post<unknown>(ENDPOINTS.requestOtp, {
      body: { roll_number: rollNumber },
    });
    if (!isOtpResponse(response)) {
      throw new ApiError('unexpected_body');
    }
  }

  async verifyOtp(
    rollNumber: string,
    code: string,
  ): Promise<SessionResponse> {
    const response = await this.client.post<unknown>(ENDPOINTS.verifyOtp, {
      body: { roll_number: rollNumber, code },
    });
    if (!isSessionResponse(response)) {
      throw new ApiError('unexpected_body');
    }
    return response;
  }

  async me(token: string): Promise<MeResponse> {
    const response = await this.client.get<unknown>(ENDPOINTS.me, { token });
    if (!isMeResponse(response)) {
      throw new ApiError('unexpected_body');
    }
    return response;
  }

  async logout(token: string): Promise<void> {
    await this.client.post<void>(ENDPOINTS.logout, { token });
  }

  async healthz(): Promise<HealthResponse> {
    const response = await this.client.get<unknown>(ENDPOINTS.healthz);
    if (!isHealthResponse(response)) {
      throw new ApiError('unexpected_body');
    }
    return response;
  }
}
