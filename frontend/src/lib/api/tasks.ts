import { apiFetch } from "./client";

export type TaskStatus =
  | "in_progress"
  | "completed"
  | "failure"
  | "queued"
  | "retry_scheduled"
  | "abandoned";

export interface TaskAttempt {
  attempt: number;
  status: TaskStatus;
  labels: Record<string, unknown>;
  result: unknown;
  error: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  next_retry_at: string | null;
  duration_ms: number | null;
}

export interface TaskRecord {
  id: string;
  name: string;
  status: TaskStatus | "unknown";
  worker: string;
  args: unknown[];
  kwargs: Record<string, unknown>;
  labels: Record<string, unknown>;
  result: unknown;
  error: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  next_retry_at: string | null;
  current_attempt: number;
  max_attempts: number;
  retry_of: string | null;
  duration_ms: number | null;
  attempts?: TaskAttempt[];
}

export interface TaskCounts {
  all: number;
  in_progress: number;
  completed: number;
  failure: number;
  queued: number;
  retry_scheduled: number;
  abandoned: number;
}

export interface TaskListResponse {
  items: TaskRecord[];
  total: number;
  counts: TaskCounts;
}

export interface TaskSchedule {
  id: string;
  name: string;
  title: string;
  enabled: boolean;
  interval_seconds: number;
  queue_name: string;
  enabled_accounts: number;
  last_task: TaskRecord | null;
}

export interface TaskSubmission {
  task_id: string;
  state: string;
  duplicate: boolean;
}

export interface FailureRetryResult {
  queued: number;
  duplicates: number;
  skipped_resolved: number;
  automatic_retrying: number;
  unsupported: number;
  failed: number;
}

export function getTasks(query: string, status: TaskStatus | null, offset = 0, limit = 50) {
  const parameters = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query.trim()) parameters.set("q", query.trim());
  if (status) parameters.set("status", status);
  return apiFetch<TaskListResponse>(`/api/task-center/tasks?${parameters}`);
}

export function getTask(taskId: string) {
  return apiFetch<TaskRecord>(`/api/task-center/tasks/${taskId}`);
}

export function rerunTask(taskId: string) {
  return apiFetch<TaskSubmission>(`/api/task-center/tasks/${taskId}/rerun`, { method: "POST" });
}

export function retryFailedTasks() {
  return apiFetch<FailureRetryResult>("/api/task-center/failures/retry", { method: "POST" });
}

export function clearAbandonedTasks() {
  return apiFetch<{ deleted: number }>("/api/task-center/tasks/abandoned", { method: "DELETE" });
}

export function getTaskSchedules() {
  return apiFetch<TaskSchedule[]>("/api/task-center/schedules");
}

export function runTaskSchedule(scheduleId: string) {
  return apiFetch<{ queued: number; duplicates: number }>(
    `/api/task-center/schedules/${scheduleId}/run`,
    { method: "POST" }
  );
}
