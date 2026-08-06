import { apiFetch } from "./client";

export interface AccountSummary {
  id: number;
  x_user_id: string;
  username: string;
  display_name: string | null;
  status: "active" | "error" | string;
  last_sync_at: string | null;
  last_error: string | null;
  post_count: number;
  description: string | null;
  profile_image_url: string | null;
  verified: boolean;
}

export interface Account extends AccountSummary {
  location: string | null;
  profile_banner_url: string | null;
  followers_count: number | null;
  following_count: number | null;
  joined_at: string | null;
}

export function getAccounts() {
  return apiFetch<AccountSummary[]>("/api/accounts");
}

export function getAccount(accountId: string) {
  return apiFetch<Account>(`/api/accounts/${accountId}`);
}
