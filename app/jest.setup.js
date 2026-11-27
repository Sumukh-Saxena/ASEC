/* eslint-env jest */

jest.mock('react-native-keychain', () => ({
  __esModule: true,
  setGenericPassword: jest.fn(async () => ({
    service: 'com.asec.app.session',
    storage: 'test',
  })),
  getGenericPassword: jest.fn(async () => false),
  resetGenericPassword: jest.fn(async () => true),
}));
