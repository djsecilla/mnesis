// Runtime configuration for the mnesis web UI.
// Edit this file in the deployed static bundle to point the UI at your API and,
// if the server is token-guarded, supply the bearer token. Tokens are NEVER
// hardcoded in source — they live here (runtime) or in the environment.
window.__MNESIS_CONFIG__ = {
  apiBase: "/api",
  token: "",
};
