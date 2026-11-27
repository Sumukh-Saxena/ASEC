import React, { useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { ApiError } from '../api/errors';
import {
  CODE_REJECTED_MESSAGE,
  EMPTY_ROLL_MESSAGE,
  INVALID_ROLL_MESSAGE,
  NEUTRAL_CODE_MESSAGE,
  NETWORK_MESSAGE,
  RATE_LIMIT_MESSAGE,
  SEND_FAILED_MESSAGE,
  SIGN_IN_FAILED_MESSAGE,
} from './messages';

export { NEUTRAL_CODE_MESSAGE } from './messages';

export interface SignInFlowProps {
  requestCode: (rollNumber: string) => Promise<void>;
  verifyCode: (rollNumber: string, code: string) => Promise<void>;
}

function messageForRequestFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === 'network' || error.kind === 'timeout') {
      return NETWORK_MESSAGE;
    }
    if (error.kind === 'rate_limited') {
      return RATE_LIMIT_MESSAGE;
    }
    if (error.kind === 'validation') {
      return INVALID_ROLL_MESSAGE;
    }
  }
  return SEND_FAILED_MESSAGE;
}

function messageForVerifyFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === 'unauthorized' || error.kind === 'validation') {
      return CODE_REJECTED_MESSAGE;
    }
    if (error.kind === 'rate_limited') {
      return RATE_LIMIT_MESSAGE;
    }
    if (error.kind === 'network' || error.kind === 'timeout') {
      return NETWORK_MESSAGE;
    }
  }
  return SIGN_IN_FAILED_MESSAGE;
}

interface RollNumberStepProps {
  requestCode: SignInFlowProps['requestCode'];
  onSent: (rollNumber: string) => void;
}

function RollNumberStep({ requestCode, onSent }: RollNumberStepProps) {
  const [rollNumber, setRollNumber] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const trimmed = rollNumber.trim();
    if (trimmed === '') {
      setMessage(EMPTY_ROLL_MESSAGE);
      return;
    }

    setBusy(true);
    setMessage(null);
    try {
      await requestCode(trimmed);
      onSent(trimmed);
    } catch (error) {
      setMessage(messageForRequestFailure(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>ASEC sign in</Text>
      <Text style={styles.body}>Enter your institute roll number.</Text>

      <TextInput
        accessibilityLabel="Roll number"
        autoCapitalize="none"
        autoCorrect={false}
        editable={!busy}
        onChangeText={setRollNumber}
        onSubmitEditing={submit}
        placeholder="Roll number"
        returnKeyType="send"
        style={styles.input}
        value={rollNumber}
      />

      {message ? (
        <Text accessibilityRole="alert" style={styles.error}>
          {message}
        </Text>
      ) : null}

      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: busy }}
        disabled={busy}
        onPress={submit}
        style={[styles.button, busy ? styles.buttonDisabled : null]}
      >
        {busy ? (
          <ActivityIndicator color="#ffffff" />
        ) : (
          <Text style={styles.buttonText}>Send code</Text>
        )}
      </Pressable>
    </View>
  );
}

interface CodeStepProps {
  rollNumber: string;
  verifyCode: SignInFlowProps['verifyCode'];
}

function CodeStep({ rollNumber, verifyCode }: CodeStepProps) {
  const [code, setCode] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const canSubmit = code.length === 6 && !busy;

  const changeCode = (value: string) => {
    setCode(value.replace(/\D/g, '').slice(0, 6));
  };

  const submit = async () => {
    if (!canSubmit) {
      return;
    }

    setBusy(true);
    setMessage(null);
    try {
      await verifyCode(rollNumber, code);
    } catch (error) {
      setMessage(messageForVerifyFailure(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>Enter code</Text>
      <Text style={styles.body}>{NEUTRAL_CODE_MESSAGE}</Text>
      <Text style={styles.meta}>Roll number: {rollNumber}</Text>

      <TextInput
        accessibilityLabel="Six-digit code"
        autoComplete="one-time-code"
        editable={!busy}
        keyboardType="number-pad"
        maxLength={6}
        onChangeText={changeCode}
        onSubmitEditing={submit}
        placeholder="123456"
        returnKeyType="done"
        style={styles.input}
        value={code}
      />

      {message ? (
        <Text accessibilityRole="alert" style={styles.error}>
          {message}
        </Text>
      ) : null}

      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: !canSubmit }}
        disabled={!canSubmit}
        onPress={submit}
        style={[styles.button, !canSubmit ? styles.buttonDisabled : null]}
      >
        {busy ? (
          <ActivityIndicator color="#ffffff" />
        ) : (
          <Text style={styles.buttonText}>Verify code</Text>
        )}
      </Pressable>
    </View>
  );
}

/**
 * The two sign-in states: roll number entry, then code entry. The signed-in
 * home is a third state owned by the session controller.
 */
export function SignInFlow({ requestCode, verifyCode }: SignInFlowProps) {
  const [rollNumber, setRollNumber] = useState<string | null>(null);

  if (rollNumber === null) {
    return (
      <RollNumberStep
        requestCode={requestCode}
        onSent={(value) => setRollNumber(value)}
      />
    );
  }

  return <CodeStep rollNumber={rollNumber} verifyCode={verifyCode} />;
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
    marginBottom: 12,
  },
  body: {
    fontSize: 16,
    color: '#374151',
    marginBottom: 16,
  },
  meta: {
    fontSize: 14,
    color: '#6b7280',
    marginBottom: 12,
  },
  input: {
    borderWidth: 1,
    borderColor: '#9ca3af',
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 18,
    color: '#111827',
    marginBottom: 12,
  },
  error: {
    color: '#b91c1c',
    fontSize: 14,
    marginBottom: 12,
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
});
