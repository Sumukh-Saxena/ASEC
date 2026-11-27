import React from 'react';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { SessionController, SessionState } from '../auth/sessionController';
import { HomeScreen } from '../screens/HomeScreen';
import { LoadingScreen } from '../screens/LoadingScreen';
import { RestoreErrorScreen } from '../screens/RestoreErrorScreen';
import { SignInFlow } from '../screens/SignInFlow';

export type RootStackParamList = {
  SignIn: undefined;
  Home: undefined;
  RestoreError: undefined;
};

const Stack = createNativeStackNavigator<RootStackParamList>();

export interface RootNavigatorProps {
  controller: SessionController;
  state: SessionState;
}

export function RootNavigator({ controller, state }: RootNavigatorProps) {
  if (state.status === 'loading') {
    return <LoadingScreen />;
  }

  return (
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        {state.status === 'signedIn' ? (
          <Stack.Screen name="Home">
            {() => (
              <HomeScreen me={state.me} onSignOut={controller.signOut} />
            )}
          </Stack.Screen>
        ) : state.status === 'restoreError' ? (
          <Stack.Screen name="RestoreError">
            {() => (
              <RestoreErrorScreen
                onRetry={controller.restore}
                onSignOut={controller.signOut}
              />
            )}
          </Stack.Screen>
        ) : (
          <Stack.Screen name="SignIn">
            {() => (
              <SignInFlow
                requestCode={controller.requestCode}
                verifyCode={controller.verifyCode}
              />
            )}
          </Stack.Screen>
        )}
      </Stack.Navigator>
    </NavigationContainer>
  );
}
