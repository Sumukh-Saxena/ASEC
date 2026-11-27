import React, { useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Person } from '../api/contract';

export interface HomeScreenProps {
  me: Person;
  onSignOut: () => Promise<void> | void;
}

/**
 * Signed-in home. Role and verticals come from the GET /me response passed in
 * by the session controller; no token is rendered or kept here.
 */
export function HomeScreen({ me, onSignOut }: HomeScreenProps) {
  const [busy, setBusy] = useState(false);
  const verticals =
    me.vertical_ids.length === 0 ? 'None' : me.vertical_ids.join(', ');

  const signOut = async () => {
    setBusy(true);
    try {
      await onSignOut();
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>Signed in</Text>
      <Text style={styles.name}>{me.name}</Text>

      <Text style={styles.label}>Role</Text>
      <Text style={styles.value}>{me.role}</Text>

      <Text style={styles.label}>Verticals</Text>
      <Text style={styles.value}>{verticals}</Text>

      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: busy }}
        disabled={busy}
        onPress={signOut}
        style={[styles.button, busy ? styles.buttonDisabled : null]}
      >
        {busy ? (
          <ActivityIndicator color="#ffffff" />
        ) : (
          <Text style={styles.buttonText}>Sign out</Text>
        )}
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
    fontSize: 26,
    fontWeight: '700',
    color: '#111827',
    marginBottom: 8,
  },
  name: {
    fontSize: 18,
    color: '#374151',
    marginBottom: 24,
  },
  label: {
    fontSize: 13,
    fontWeight: '600',
    color: '#6b7280',
    textTransform: 'uppercase',
    marginTop: 12,
  },
  value: {
    fontSize: 20,
    color: '#111827',
    marginTop: 2,
  },
  button: {
    backgroundColor: '#b91c1c',
    borderRadius: 8,
    paddingVertical: 12,
    alignItems: 'center',
    marginTop: 32,
  },
  buttonDisabled: {
    backgroundColor: '#fca5a5',
  },
  buttonText: {
    color: '#ffffff',
    fontSize: 16,
    fontWeight: '600',
  },
});
