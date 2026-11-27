module.exports = {
  preset: '@react-native/jest-preset',
  setupFiles: ['<rootDir>/jest.setup.js'],
  testTimeout: 15000,
  testPathIgnorePatterns: [
    '/node_modules/',
    '/android/',
    '<rootDir>/mock-server/',
  ],
};
