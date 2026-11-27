import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { ApiClient } from './src/api/client';
import { HttpAuthApi } from './src/auth/authApi';
import {
  SessionController,
  SessionControllerOptions,
} from './src/auth/sessionController';
import { API_BASE_URL } from './src/config';
import { RootNavigator } from './src/navigation/RootNavigator';
import { KeychainSecureStorage } from './src/session/keychainSecureStorage';
import { SessionStore } from './src/session/sessionStore';

export function createDefaultController(): SessionController {
  const store = new SessionStore(new KeychainSecureStorage());
  let controller!: SessionController;
  const client = new ApiClient({
    baseUrl: API_BASE_URL,
    onUnauthorized: () => controller.handleUnauthorized(),
  });
  const api = new HttpAuthApi(client);
  const options: SessionControllerOptions = { api, store };
  controller = new SessionController(options);
  return controller;
}

export interface AppProps {
  controller?: SessionController;
}

export default function App({ controller: provided }: AppProps = {}) {
  const [controller] = useState(
    () => provided ?? createDefaultController(),
  );
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );

  useEffect(() => {
    controller.restore().catch(() => undefined);
  }, [controller]);

  return (
    <SafeAreaProvider>
      <RootNavigator controller={controller} state={state} />
    </SafeAreaProvider>
  );
}
