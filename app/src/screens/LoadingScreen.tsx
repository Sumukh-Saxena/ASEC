import React from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';

export function LoadingScreen() {
  return (
    <View style={styles.screen}>
      <ActivityIndicator color="#1d4ed8" />
      <Text style={styles.text}>Loading</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: '#ffffff',
  },
  text: {
    marginTop: 12,
    fontSize: 16,
    color: '#374151',
  },
});
