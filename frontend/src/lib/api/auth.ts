import { apiFetch } from "./client";

export interface Session {
  authenticated: boolean;
}

export function getSession() {
  return apiFetch<Session>("/api/auth/session");
}

export function createSession(token: string) {
  return apiFetch<void>("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token })
  });
}

export function deleteSession() {
  return apiFetch<void>("/api/auth/session", { method: "DELETE" });
}
