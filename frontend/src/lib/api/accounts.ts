import { apiFetch } from "./client";

export interface AccountSummary {
  x_user_id: string;
  current_username: string | null;
  display_name: string | null;
  archive_enabled: boolean;
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

export interface ResolvedAccount {
  x_user_id: string;
  current_username: string;
  display_name: string | null;
  profile_image_url: string | null;
  description: string | null;
  already_archived: boolean;
  archive_enabled: boolean | null;
}

export interface UsernameHistoryItem {
  id: number;
  x_user_id: string;
  username: string;
  observed_from: string;
  observed_to: string | null;
  last_observed_at: string;
}

export function getAccounts() {
  return apiFetch<AccountSummary[]>("/api/accounts");
}

export function getAccount(xUserId: string) {
  return apiFetch<Account>(`/api/accounts/${xUserId}`);
}

export function resolveAccount(query: string) {
  return apiFetch<ResolvedAccount>("/api/accounts/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query })
  });
}

export function addAccount(account: ResolvedAccount) {
  return apiFetch<Account>("/api/accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      x_user_id: account.x_user_id,
      current_username: account.current_username,
      display_name: account.display_name
    })
  });
}

export function setAccountEnabled(xUserId: string, archiveEnabled: boolean) {
  return apiFetch<Account>(`/api/accounts/${xUserId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archive_enabled: archiveEnabled })
  });
}

export function syncAccount(xUserId: string) {
  return apiFetch<{ status: string; error: string | null }>(`/api/accounts/${xUserId}/sync`, {
    method: "POST"
  });
}

export function getUsernameHistory(xUserId: string) {
  return apiFetch<UsernameHistoryItem[]>(`/api/accounts/${xUserId}/username-history`);
}
