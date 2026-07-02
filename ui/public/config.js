// Runtime configuration for the mnesis web UI.
// Edit this file in the deployed static bundle to point the UI at your API base.
//
// IAM5: there is no API token here anymore. The browser authenticates with a real
// login (username + password) that sets an httpOnly session cookie; the backend
// enforces per-user auth + the policy decision point on every /api request.
window.__MNESIS_CONFIG__ = {
  apiBase: "/api",
};
