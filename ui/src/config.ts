// Resolve the API base. Runtime config (public/config.js -> window) wins so the
// static bundle is generic; build-time env vars are fallbacks. No hardcoding.
//
// IAM5: the browser no longer carries an API token. Identity is a real login +
// an httpOnly session cookie (see src/api/client.ts); nginx just forwards it.
const runtime = window.__MNESIS_CONFIG__ ?? {};

export const API_BASE: string = runtime.apiBase ?? import.meta.env.VITE_API_BASE ?? "/api";
