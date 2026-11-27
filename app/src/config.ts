/**
 * The only place the app reads its API base URL.
 *
 * The value comes from the untracked `src/config.local.ts`, which is created
 * from `src/config.local.example.ts` by `scripts/ensure-local-config.js`.
 * The example's committed default is a clearly fake placeholder, never a real
 * deployment URL.
 */
import { API_BASE_URL as LOCAL_API_BASE_URL } from './config.local';

export const API_BASE_URL = LOCAL_API_BASE_URL;
