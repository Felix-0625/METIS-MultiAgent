/** Use the reverse-proxied API by default in Docker/Railway; Vite can override it. */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '/api';
