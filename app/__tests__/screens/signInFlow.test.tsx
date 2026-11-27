import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react-native';
import { ApiError } from '../../src/api/errors';
import {
  NEUTRAL_CODE_MESSAGE,
  SignInFlow,
} from '../../src/screens/SignInFlow';

async function goToCodeScreen(
  requestCode = jest.fn(async () => undefined),
  verifyCode = jest.fn(async () => undefined),
) {
  await render(<SignInFlow requestCode={requestCode} verifyCode={verifyCode} />);
  await fireEvent.changeText(screen.getByLabelText('Roll number'), 'R1');
  await fireEvent.press(screen.getByText('Send code'));
  await screen.findByText(NEUTRAL_CODE_MESSAGE);
  return { requestCode, verifyCode };
}

describe('roll number step', () => {
  it('uses the exact required neutral OTP message', () => {
    expect(NEUTRAL_CODE_MESSAGE).toBe(
      'If this roll number is registered, a code was sent on Discord.',
    );
  });

  it('shows the same neutral message for known and unknown roll numbers', async () => {
    const first = await goToCodeScreen(jest.fn(async () => undefined));
    expect(screen.getByText(NEUTRAL_CODE_MESSAGE)).toBeTruthy();
    expect(
      screen.queryByText(/not registered|unknown roll|does not exist/i),
    ).toBeNull();
    first.requestCode.mockClear();
    await screen.unmount();

    const second = await goToCodeScreen(jest.fn(async () => undefined));
    expect(screen.getByText(NEUTRAL_CODE_MESSAGE)).toBeTruthy();
    expect(
      screen.queryByText(/not registered|unknown roll|does not exist/i),
    ).toBeNull();
    expect(second.requestCode).toHaveBeenCalledWith('R1');
  });

  it('rejects an empty roll number before calling the API', async () => {
    const requestCode = jest.fn(async () => undefined);
    await render(<SignInFlow requestCode={requestCode} verifyCode={jest.fn()} />);

    await fireEvent.press(screen.getByText('Send code'));

    expect(await screen.findByText('Enter your roll number.')).toBeTruthy();
    expect(requestCode).not.toHaveBeenCalled();
  });

  it('shows a retryable message when requesting a code fails on the network', async () => {
    const requestCode = jest.fn(async () => {
      throw new ApiError('network');
    });
    await render(<SignInFlow requestCode={requestCode} verifyCode={jest.fn()} />);

    await fireEvent.changeText(screen.getByLabelText('Roll number'), 'R1');
    await fireEvent.press(screen.getByText('Send code'));

    expect(
      await screen.findByText(
        'Could not reach the server. Check your connection and try again.',
      ),
    ).toBeTruthy();
    expect(screen.getByLabelText('Roll number').props.value).toBe('R1');
  });
});

describe('code step', () => {
  it('accepts exactly six digits and filters everything else', async () => {
    await goToCodeScreen();
    const input = screen.getByLabelText('Six-digit code');

    await fireEvent.changeText(input, '12345');
    expect(screen.getByRole('button', { name: 'Verify code' })).toBeDisabled();

    await fireEvent.changeText(input, '1234567');
    expect(screen.getByLabelText('Six-digit code').props.value).toBe('123456');
    expect(screen.getByRole('button', { name: 'Verify code' })).toBeEnabled();

    await fireEvent.changeText(input, '12a34b56');
    expect(screen.getByLabelText('Six-digit code').props.value).toBe('123456');

    await fireEvent.changeText(input, '12345');
    expect(screen.getByRole('button', { name: 'Verify code' })).toBeDisabled();
  });

  it('shows a generic message for a rejected code', async () => {
    const verifyCode = jest.fn(async () => {
      throw new ApiError('unauthorized', 401);
    });
    await goToCodeScreen(undefined, verifyCode);

    await fireEvent.changeText(screen.getByLabelText('Six-digit code'), '123456');
    await fireEvent.press(screen.getByText('Verify code'));

    expect(await screen.findByText('Code not accepted')).toBeTruthy();
    expect(verifyCode).toHaveBeenCalledWith('R1', '123456');
  });

  it('shows the rate-limit message for a 429', async () => {
    const verifyCode = jest.fn(async () => {
      throw new ApiError('rate_limited', 429);
    });
    await goToCodeScreen(undefined, verifyCode);

    await fireEvent.changeText(screen.getByLabelText('Six-digit code'), '123456');
    await fireEvent.press(screen.getByText('Verify code'));

    expect(
      await screen.findByText('Too many attempts, try again later'),
    ).toBeTruthy();
  });

  it('keeps the entered code and shows a retryable message on a network failure', async () => {
    const verifyCode = jest.fn(async () => {
      throw new ApiError('network');
    });
    await goToCodeScreen(undefined, verifyCode);

    await fireEvent.changeText(screen.getByLabelText('Six-digit code'), '123456');
    await fireEvent.press(screen.getByText('Verify code'));

    expect(
      await screen.findByText(
        'Could not reach the server. Check your connection and try again.',
      ),
    ).toBeTruthy();
    expect(screen.getByLabelText('Six-digit code').props.value).toBe('123456');
  });
});
