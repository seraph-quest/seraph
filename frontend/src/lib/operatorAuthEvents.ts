export const AUTH_REQUIRED_EVENT = "seraph-auth-required";

export function signalAuthRequired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
}
