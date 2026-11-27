import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react-native';
import { HomeScreen } from '../../src/screens/HomeScreen';
import { RestoreErrorScreen } from '../../src/screens/RestoreErrorScreen';

describe('HomeScreen', () => {
  it('shows the role and verticals returned by GET /me and signs out', async () => {
    const onSignOut = jest.fn(async () => undefined);
    await render(
      <HomeScreen
        me={{
          id: 7,
          name: 'Test Person',
          role: 'HEAD',
          vertical_ids: [4, 5],
        }}
        onSignOut={onSignOut}
      />,
    );

    expect(screen.getByText('HEAD')).toBeTruthy();
    expect(screen.getByText('4, 5')).toBeTruthy();
    expect(screen.queryByText('tok_')).toBeNull();

    await fireEvent.press(screen.getByText('Sign out'));
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it('shows None when the person has no verticals', async () => {
    await render(
      <HomeScreen
        me={{ id: 7, name: 'Test Person', role: 'ADMIN', vertical_ids: [] }}
        onSignOut={jest.fn()}
      />,
    );

    expect(screen.getByText('None')).toBeTruthy();
  });
});

describe('RestoreErrorScreen', () => {
  it('offers retry and local sign-out', async () => {
    const onRetry = jest.fn();
    const onSignOut = jest.fn();
    await render(<RestoreErrorScreen onRetry={onRetry} onSignOut={onSignOut} />);

    expect(
      screen.getByText(
        'Could not reach the server. Check your connection and try again.',
      ),
    ).toBeTruthy();

    await fireEvent.press(screen.getByText('Retry'));
    await fireEvent.press(screen.getByText('Sign out'));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });
});
