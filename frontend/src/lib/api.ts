import { AUTH_REQUIRED_EVENT } from "./operatorAuthEvents";

/**
 * Send an API request with the operator session cookie attached.
 *
 * The local cockpit and backend use different ports, so fetch's default
 * `same-origin` credentials mode would silently omit the authenticated cookie.
 * Keeping this at one boundary also lets an expired/revoked session return the
 * mounted cockpit to its login screen without putting tokens in JavaScript.
 */
export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(input, {
    ...init,
    credentials: init.credentials ?? "include",
  });
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
  return response;
}
