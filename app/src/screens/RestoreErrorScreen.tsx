import React, { useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { NETWORK_MESSAGE } from './messages';

export interface RestoreErrorScreenProps {
  onRetry: () => Promise<void> | void;
  onSignOut: () => Promise<void> | void;
}

/**
 * App-start retry state. The app deliberately does not store a role or
 * verticals it wrote itself, so a network failure on GET /me shows this screen
 * instead of an offline home with stale authority data.
 */
export function RestoreErrorScreen({
  onRetry,
  onSignOut,
}: RestoreErrorScreenProps) {
  const [busy, setBusy] = useState(false);

  const retry = async () => {
    setBusy(true);
    try {
      await onRetry();
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>Could not load your profile</Text>
      <Text style={styles.body}>{NETWORK_MESSAGE}</Text>

      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: busy }}
        disabled={busy}
        onPress={retry}
        style={[styles.button, busy ? styles.buttonDisabled : null]}
      >
        {busy ? (
          <ActivityIndicator color="#ffffff" />
        ) : (
          <Text style={styles.buttonText}>Retry</Text>
        )}
      </Pressable>

      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: busy }}
        disabled={busy}
        onPress={onSignOut}
        style={styles.secondaryButton}
      >
        <Text style={styles.secondaryText}>Sign out</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    justifyContent: 'center',
    padding: 24,
    backgroundColor: '#ffffff',
  },
  title: {
    fontSize: 24,
    fontWeight: '700',
    color: '#111827',
    marginBottom: 12,
  },
  body: {
    fontSize: 16,
    color: '#374151',
    marginBottom: 24,
  },
  button: {
    backgroundColor: '#1d4ed8',
    borderRadius: 8,
    paddingVertical: 12,
    alignItems: 'center',
  },
  buttonDisabled: {
    backgroundColor: '#93c5fd',
  },
  buttonText: {
    color: '#ffffff',
    fontSize: 16,
    fontWeight: '600',
  },
  secondaryButton: {
    paddingVertical: 12,
    alignItems: 'center',
    marginTop: 8,
  },
  secondaryText: {
    color: '#1d4ed8',
    fontSize: 16,
    fontWeight: '600',
  },
});
