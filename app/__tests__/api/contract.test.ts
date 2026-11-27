import { ENDPOINTS } from '../../src/api/contract';

describe('contract endpoints', () => {
  it('matches the exported OpenAPI paths and operations', () => {
    expect(ENDPOINTS.requestOtp).toMatchObject({
      method: 'POST',
      path: '/auth/otp',
      operation: 'request_otp_auth_otp_post',
    });
    expect(ENDPOINTS.verifyOtp).toMatchObject({
      method: 'POST',
      path: '/auth/verify',
      operation: 'verify_otp_auth_verify_post',
    });
    expect(ENDPOINTS.logout).toMatchObject({
      method: 'POST',
      path: '/auth/logout',
      operation: 'logout_auth_logout_post',
    });
    expect(ENDPOINTS.me).toMatchObject({
      method: 'GET',
      path: '/me',
      operation: 'me_me_get',
    });
    expect(ENDPOINTS.healthz).toMatchObject({
      method: 'GET',
      path: '/healthz',
      operation: 'healthz_healthz_get',
    });
  });
});
