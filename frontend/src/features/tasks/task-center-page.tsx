import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import type { ReactNode } from "react";
import {
  FiActivity,
  FiAlertCircle,
  FiArrowLeft,
  FiCheckCircle,
  FiChevronLeft,
  FiChevronRight,
  FiClock,
  FiInbox,
  FiPlay,
  FiRefreshCw,
  FiSearch,
  FiServer
} from "react-icons/fi";

import {
  TaskRecord,
  TaskStatus,
  getTask,
  getTaskSchedules,
  getTasks,
  rerunTask,
  retryFailedTasks,
  runTaskSchedule
} from "../../lib/api/tasks";
import "./task-center-page.css";

const STATUS_META: Record<string, { label: string; className: string }> = {
  in_progress: { label: "运行中", className: "is-running" },
  completed: { label: "已完成", className: "is-completed" },
  failure: { label: "失败", className: "is-failure" },
  queued: { label: "等待中", className: "is-queued" },
  abandoned: { label: "已中断", className: "is-abandoned" },
  unknown: { label: "未知", className: "is-abandoned" }
};

const TASK_NAMES: Record<string, string> = {
  "archivex.sync_account": "账号同步",
  "archivex.download_media": "媒体下载",
  "archivex.schedule_enabled_accounts": "定时同步分发"
};

export function TaskListPage() {
  const queryClient = useQueryClient();
  const pageSize = 50;
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<TaskStatus | null>(null);
  const [page, setPage] = useState(0);
  const tasks = useQuery({
    queryKey: ["task-center", query, status, page],
    queryFn: () => getTasks(query, status, page * pageSize, pageSize),
    refetchInterval: 5_000
  });
  const retryFailures = useMutation({
    mutationFn: retryFailedTasks,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["task-center"] });
    }
  });

  const filters: Array<{ value: TaskStatus | null; label: string }> = [
    { value: null, label: "全部" },
    { value: "in_progress", label: "运行中" },
    { value: "queued", label: "等待中" },
    { value: "completed", label: "已完成" },
    { value: "failure", label: "失败" },
    { value: "abandoned", label: "已中断" }
  ];

  return (
    <div className="x-task-page">
      <TaskHeader title="任务中心" refreshing={tasks.isFetching} onRefresh={() => void tasks.refetch()} />
      <TaskNavigation active="tasks" />

      <section className="x-task-metrics" aria-label="队列状态概览">
        <Metric icon={<FiActivity />} label="运行中" value={tasks.data?.counts.in_progress} tone="running" />
        <Metric icon={<FiInbox />} label="等待中" value={tasks.data?.counts.queued} tone="queued" />
        <Metric icon={<FiCheckCircle />} label="已完成" value={tasks.data?.counts.completed} tone="completed" />
        <Metric icon={<FiAlertCircle />} label="异常" value={(tasks.data?.counts.failure ?? 0) + (tasks.data?.counts.abandoned ?? 0)} tone="failure" />
      </section>

      <section className="x-task-controls">
        <label className="x-task-search">
          <FiSearch aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => { setQuery(event.target.value); setPage(0); }}
            placeholder="搜索任务名称或 ID"
            aria-label="搜索任务"
          />
        </label>
        <div className="x-task-filters" aria-label="任务状态筛选">
          {filters.map((filter) => (
            <button
              key={filter.label}
              type="button"
              className={status === filter.value ? "is-active" : ""}
              onClick={() => { setStatus(filter.value); setPage(0); }}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </section>

      {status === "failure" && (
        <section className="x-task-failure-action" aria-live="polite">
          <div>
            <strong>{tasks.data?.counts.failure ?? 0} 条失败记录</strong>
          </div>
          <button
            type="button"
            className="x-task-command"
            disabled={retryFailures.isPending || (tasks.data?.counts.failure ?? 0) === 0}
            onClick={() => retryFailures.mutate()}
          >
            <FiRefreshCw className={retryFailures.isPending ? "is-spinning" : ""} />
            <span>{retryFailures.isPending ? "提交中" : "一键重试"}</span>
          </button>
        </section>
      )}
      {status === "failure" && retryFailures.data && (
        <div className={retryFailures.data.failed ? "x-task-action-error" : "x-task-action-success"}>
          {failureRetryMessage(retryFailures.data)}
        </div>
      )}
      {status === "failure" && retryFailures.error && (
        <div className="x-task-action-error">{retryFailures.error.message}</div>
      )}

      <section className="x-task-list" aria-live="polite">
        <div className="x-task-list-head">
          <span>任务</span><span>状态</span><span>队列</span><span>耗时</span><span>开始时间</span><span />
        </div>
        {tasks.isPending && <TaskState message="正在读取任务..." />}
        {tasks.error && <TaskState message={tasks.error.message} error />}
        {tasks.data?.items.length === 0 && <TaskState message="没有符合条件的任务" />}
        {tasks.data?.items.map((task) => <TaskRow key={task.id} task={task} />)}
        {tasks.data && tasks.data.total > pageSize && (
          <div className="x-task-pagination">
            <span>第 {page + 1} 页 · 共 {tasks.data.total} 条</span>
            <div>
              <button type="button" disabled={page === 0} onClick={() => setPage((value) => value - 1)} aria-label="上一页" title="上一页"><FiChevronLeft /></button>
              <button type="button" disabled={(page + 1) * pageSize >= tasks.data.total} onClick={() => setPage((value) => value + 1)} aria-label="下一页" title="下一页"><FiChevronRight /></button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

export function TaskDetailsPage({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const task = useQuery({
    queryKey: ["task-center", "detail", taskId],
    queryFn: () => getTask(taskId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "in_progress" ? 2_000 : false;
    }
  });
  const rerun = useMutation({
    mutationFn: () => rerunTask(taskId),
    onSuccess: async (submission) => {
      await queryClient.invalidateQueries({ queryKey: ["task-center"] });
      await navigate({ to: "/tasks/$taskId", params: { taskId: submission.task_id } });
    }
  });

  return (
    <div className="x-task-page">
      <header className="x-task-header x-task-detail-header">
        <Link to="/tasks" className="x-task-back" aria-label="返回任务列表" title="返回">
          <FiArrowLeft />
        </Link>
        <div><h1>任务详情</h1><span>{shortId(taskId)}</span></div>
        {task.data && canRerunTask(task.data) && (
          <button
            type="button"
            className="x-task-command"
            disabled={rerun.isPending}
            onClick={() => rerun.mutate()}
          >
            <FiRefreshCw /><span>{rerun.isPending ? "提交中" : "重新执行"}</span>
          </button>
        )}
      </header>

      {task.isPending && <TaskState message="正在读取任务详情..." />}
      {task.error && <TaskState message={task.error.message} error />}
      {task.data && <TaskDetail task={task.data} />}
      {rerun.error && <div className="x-task-action-error">{rerun.error.message}</div>}
    </div>
  );
}

export function TaskSchedulesPage() {
  const queryClient = useQueryClient();
  const schedules = useQuery({ queryKey: ["task-center", "schedules"], queryFn: getTaskSchedules });
  const run = useMutation({
    mutationFn: runTaskSchedule,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["task-center"] });
    }
  });

  return (
    <div className="x-task-page">
      <TaskHeader title="任务中心" refreshing={schedules.isFetching} onRefresh={() => void schedules.refetch()} />
      <TaskNavigation active="schedules" />
      <section className="x-schedule-summary">
        <div><FiClock /><span>活动计划</span><strong>{schedules.data?.filter((item) => item.enabled).length ?? 0}</strong></div>
        <div><FiServer /><span>归档账号</span><strong>{schedules.data?.[0]?.enabled_accounts ?? 0}</strong></div>
      </section>
      <section className="x-schedule-list" aria-live="polite">
        {schedules.isPending && <TaskState message="正在读取定时任务..." />}
        {schedules.error && <TaskState message={schedules.error.message} error />}
        {schedules.data?.map((schedule) => (
          <article className="x-schedule-row" key={schedule.id}>
            <span className="x-schedule-icon"><FiClock /></span>
            <div className="x-schedule-main">
              <div className="x-schedule-title">
                <strong>{schedule.title}</strong>
                <span className={schedule.enabled ? "is-enabled" : ""}>{schedule.enabled ? "已启用" : "已停用"}</span>
              </div>
              <span>每 {formatInterval(schedule.interval_seconds)} · {queueLabel(schedule.queue_name)}</span>
              <span>{schedule.last_task ? `上次运行 ${formatDate(schedule.last_task.started_at ?? schedule.last_task.queued_at)}` : "尚未记录运行"}</span>
            </div>
            <button
              type="button"
              className="x-task-command"
              disabled={run.isPending}
              onClick={() => run.mutate(schedule.id)}
            >
              <FiPlay /><span>{run.isPending ? "提交中" : "立即运行"}</span>
            </button>
          </article>
        ))}
      </section>
      {run.data && <div className="x-task-action-success">已提交 {run.data.queued} 个账号，{run.data.duplicates} 个已在队列中</div>}
      {run.error && <div className="x-task-action-error">{run.error.message}</div>}
    </div>
  );
}

function TaskHeader({ title, refreshing, onRefresh }: { title: string; refreshing: boolean; onRefresh: () => void }) {
  return (
    <header className="x-task-header">
      <div><h1>{title}</h1><span>队列运行状态</span></div>
      <button type="button" className="x-icon-button" onClick={onRefresh} aria-label="刷新任务" title="刷新">
        <FiRefreshCw className={refreshing ? "is-spinning" : ""} />
      </button>
    </header>
  );
}

function TaskNavigation({ active }: { active: "tasks" | "schedules" }) {
  return (
    <nav className="x-task-nav" aria-label="任务中心视图">
      <Link to="/tasks" className={active === "tasks" ? "is-active" : ""}>任务</Link>
      <Link to="/tasks/schedules" className={active === "schedules" ? "is-active" : ""}>定时任务</Link>
    </nav>
  );
}

function Metric({ icon, label, value, tone }: { icon: ReactNode; label: string; value?: number; tone: string }) {
  return <div className={`x-task-metric is-${tone}`}><span>{icon}</span><div><strong>{value ?? "-"}</strong><small>{label}</small></div></div>;
}

function TaskRow({ task }: { task: TaskRecord }) {
  const status = STATUS_META[task.status] ?? STATUS_META.unknown;
  return (
    <Link to="/tasks/$taskId" params={{ taskId: task.id }} className="x-task-row">
      <span className="x-task-name"><strong>{taskLabel(task.name)}</strong><small>{shortId(task.id)}</small></span>
      <span><StatusBadge taskStatus={task.status} /></span>
      <span className="x-task-queue">{queueLabel(task.worker)}</span>
      <span className="x-task-duration">{formatDuration(task.duration_ms)}</span>
      <time dateTime={task.started_at ?? task.queued_at ?? undefined}>{formatDate(task.started_at ?? task.queued_at)}</time>
      <FiChevronRight aria-label={status.label} />
    </Link>
  );
}

function TaskDetail({ task }: { task: TaskRecord }) {
  return (
    <div className="x-task-detail">
      <section className="x-task-detail-lead">
        <span className="x-task-detail-icon"><FiServer /></span>
        <div><h2>{taskLabel(task.name)}</h2><code>{task.name}</code></div>
        <StatusBadge taskStatus={task.status} />
      </section>
      <dl className="x-task-facts">
        <div><dt>任务 ID</dt><dd><code>{task.id}</code></dd></div>
        <div><dt>执行队列</dt><dd>{queueLabel(task.worker)}</dd></div>
        <div><dt>进入队列</dt><dd>{formatDate(task.queued_at, true)}</dd></div>
        <div><dt>开始时间</dt><dd>{formatDate(task.started_at, true)}</dd></div>
        <div><dt>完成时间</dt><dd>{formatDate(task.finished_at, true)}</dd></div>
        <div><dt>执行耗时</dt><dd>{formatDuration(task.duration_ms)}</dd></div>
      </dl>
      {task.error && <DetailBlock title="错误" value={task.error} error />}
      <DetailBlock title="参数" value={{ args: task.args, kwargs: task.kwargs }} />
      {task.result !== null && <DetailBlock title="执行结果" value={task.result} />}
    </div>
  );
}

function DetailBlock({ title, value, error = false }: { title: string; value: unknown; error?: boolean }) {
  return (
    <section className={`x-task-detail-block ${error ? "is-error" : ""}`}>
      <h3>{title}</h3>
      <pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>
    </section>
  );
}

function StatusBadge({ taskStatus }: { taskStatus: string }) {
  const status = STATUS_META[taskStatus] ?? STATUS_META.unknown;
  return <span className={`x-task-status ${status.className}`}><i />{status.label}</span>;
}

function TaskState({ message, error = false }: { message: string; error?: boolean }) {
  return <div className={`x-task-state ${error ? "is-error" : ""}`}>{error ? <FiAlertCircle /> : <FiInbox />}<span>{message}</span></div>;
}

function taskLabel(name: string) { return TASK_NAMES[name] ?? name; }
function canRerunTask(task: TaskRecord) {
  return task.name === "archivex.sync_account" || task.name === "archivex.download_media";
}
function shortId(id: string) { return id.split("-")[0]; }
function queueLabel(queue: string) {
  if (queue.includes("media")) return "媒体队列";
  if (queue.includes("crawl")) return "抓取队列";
  return queue || "等待分配";
}
function formatDuration(milliseconds: number | null) {
  if (milliseconds === null) return "-";
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  return `${Math.floor(milliseconds / 60_000)}m ${Math.round(milliseconds % 60_000 / 1_000)}s`;
}
function formatDate(value: string | null, detailed = false) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", detailed
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
  ).format(new Date(value));
}
function formatInterval(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

function failureRetryMessage(result: {
  queued: number;
  duplicates: number;
  skipped_resolved: number;
  automatic_retrying: number;
  unsupported: number;
  failed: number;
}) {
  const parts = [`已提交 ${result.queued} 个任务`];
  if (result.duplicates) parts.push(`${result.duplicates} 个已在队列中`);
  if (result.skipped_resolved) parts.push(`${result.skipped_resolved} 个已恢复并跳过`);
  if (result.automatic_retrying) parts.push(`${result.automatic_retrying} 个仍在自动重试`);
  if (result.unsupported) parts.push(`${result.unsupported} 个暂不支持`);
  if (result.failed) parts.push(`${result.failed} 个提交失败`);
  return parts.join("，");
}
