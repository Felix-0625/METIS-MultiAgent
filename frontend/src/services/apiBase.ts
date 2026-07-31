/** Resolve the API origin without crossing localhost/127.0.0.1 boundaries. */
export const API_BASE_URL = import.meta.env.VITE_API_URL || (
  typeof window !== 'undefined'
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : 'http://localhost:8000'
);
