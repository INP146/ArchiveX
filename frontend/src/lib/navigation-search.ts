import type { TaskStatus } from "./api/tasks";

// X IDs exceed JavaScript's safe integer range. Keep URL values as strings.
export function parseArchiveSearch(value: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(value));
}

export function stringifyArchiveSearch(search: Record<string, unknown>): string {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) {
    if (value !== undefined && value !== null) parameters.set(key, String(value));
  }
  return parameters.size ? `?${parameters}` : "";
}

function textParameter(value: unknown, maxLength: number) {
  return typeof value === "string" && value.trim() ? value.slice(0, maxLength) : undefined;
}

function accountParameter(value: unknown) {
  return typeof value === "string" && /^\d+$/.test(value) ? value : undefined;
}

export interface PostSearch {
  q?: string;
  account?: string;
}

export function validatePostSearch(search: Record<string, unknown>): PostSearch {
  return { q: textParameter(search.q, 500), account: accountParameter(search.account) };
}

export interface TaskSearch {
  q?: string;
  account?: string;
  status?: TaskStatus;
  page?: number;
}

export function validateTaskSearch(search: Record<string, unknown>): TaskSearch {
  const statuses: TaskStatus[] = ["in_progress", "completed", "failure", "queued", "retry_scheduled", "abandoned"];
  const page = Number(search.page);
  return {
    q: textParameter(search.q, 200),
    account: accountParameter(search.account),
    status: statuses.includes(search.status as TaskStatus) ? search.status as TaskStatus : undefined,
    page: Number.isSafeInteger(page) && page > 0 ? page : undefined
  };
}
