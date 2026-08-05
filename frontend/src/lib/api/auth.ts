import { apiFetch } from "./client";

export interface SessionUser {
  display_name: string;
  username: string;
  avatar_url: string | null;
}

export interface Session {
  authenticated: boolean;
  user: SessionUser | null;
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
