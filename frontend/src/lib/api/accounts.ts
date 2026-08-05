import { apiFetch } from "./client";

export interface Account {
  id: number;
  x_user_id: string;
  username: string;
  display_name: string | null;
  status: "active" | "error" | string;
  last_sync_at: string | null;
  last_error: string | null;
  post_count: number;
}

export function getAccounts() {
  return apiFetch<Account[]>("/api/accounts");
}
