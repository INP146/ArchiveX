import { apiFetch } from "./client";

export interface CrawlerAccount {
  username: string;
  active: boolean;
  proxy_configured: boolean;
  proxy_url: string | null;
  last_used: string | null;
  total_requests: number;
}

export function getCrawlerAccounts() {
  return apiFetch<CrawlerAccount[]>("/api/crawler-accounts");
}

export function importCrawlerAccountCookies(username: string, cookies: string, replace: boolean) {
  return apiFetch<CrawlerAccount>("/api/crawler-accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, cookies, replace })
  });
}

export function setCrawlerAccountProxy(username: string, proxy: string | null) {
  return apiFetch<CrawlerAccount>(`/api/crawler-accounts/${encodeURIComponent(username)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proxy })
  });
}
