// Resolve API base + token. Runtime config (public/config.js -> window) wins so
// the static bundle is generic; build-time env vars are fallbacks. No hardcoding.
const runtime = window.__MNESIS_CONFIG__ ?? {};

export const API_BASE: string = runtime.apiBase ?? import.meta.env.VITE_API_BASE ?? "/api";
export const API_TOKEN: string = runtime.token ?? import.meta.env.VITE_API_TOKEN ?? "";
