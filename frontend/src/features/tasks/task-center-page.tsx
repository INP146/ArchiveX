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
  FiExternalLink,
  FiGitBranch,
  FiImage,
  FiInbox,
  FiPlay,
  FiRefreshCw,
  FiSearch,
  FiServer,
  FiTrash2,
  FiUser
} from "react-icons/fi";

import {
  TaskRecord,
  TaskStatus,
  TerminalTaskStatus,
  clearTaskHistory,
  getTask,
  getTaskSchedules,
  getTaskSummary,
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
  retry_scheduled: { label: "等待重试", className: "is-retrying" },
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
  const summary = useQuery({
    queryKey: ["task-center", "summary"],
    queryFn: getTaskSummary,
    refetchInterval: 2_000,
    staleTime: 0
  });
  const tasks = useQuery({
    queryKey: ["task-center", "list", query, status, page],
    queryFn: () => getTasks(query, status, page * pageSize, pageSize),
    refetchInterval: 2_000,
    staleTime: 0,
    gcTime: 0
  });
  const retryFailures = useMutation({
    mutationFn: retryFailedTasks,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["task-center"] });
    }
  });
  const clearHistory = useMutation({
    mutationFn: (historyStatus?: TerminalTaskStatus) => clearTaskHistory(historyStatus),
    onSuccess: async () => {
      setPage(0);
      await queryClient.invalidateQueries({ queryKey: ["task-center"] });
    }
  });

  const selectStatus = (value: TaskStatus | null) => {
    clearHistory.reset();
    setStatus(value);
    setPage(0);
  };

  const historyStatus = terminalTaskStatus(status);
  const canClearHistory = status === null || historyStatus !== undefined;
  const historyCount = status === null
    ? terminalTaskCount(summary.data?.counts)
    : (summary.data?.counts[status] ?? 0);
  const historyLabel = status === null
    ? "已结束记录"
    : `${STATUS_META[status]?.label ?? "任务"}记录`;
  const requestHistoryClear = () => {
    if (!window.confirm(`确定清除全部${historyLabel}？此操作不可撤销。`)) return;
    clearHistory.mutate(historyStatus);
  };

  const filters: Array<{ value: TaskStatus | null; label: string }> = [
    { value: null, label: "全部" },
    { value: "in_progress", label: "运行中" },
    { value: "queued", label: "等待中" },
    { value: "retry_scheduled", label: "等待重试" },
    { value: "completed", label: "已完成" },
    { value: "failure", label: "失败" },
    { value: "abandoned", label: "已中断" }
  ];

  return (
    <div className="x-task-page">
      <TaskHeader
        title="任务中心"
        refreshing={tasks.isFetching || summary.isFetching}
        onRefresh={() => void Promise.all([tasks.refetch(), summary.refetch()])}
      />
      <TaskNavigation active="tasks" />

      <section className="x-task-metrics" aria-label="队列状态概览">
        <Metric icon={<FiActivity />} label="运行中" value={summary.data?.counts.in_progress} tone="running" active={status === "in_progress"} onClick={() => selectStatus("in_progress")} />
        <Metric icon={<FiInbox />} label="等待中" value={summary.data?.counts.queued} tone="queued" active={status === "queued"} onClick={() => selectStatus("queued")} />
        <Metric icon={<FiClock />} label="等待重试" value={summary.data?.counts.retry_scheduled} tone="retrying" active={status === "retry_scheduled"} onClick={() => selectStatus("retry_scheduled")} />
        <Metric icon={<FiCheckCircle />} label="已完成" value={summary.data?.counts.completed} tone="completed" active={status === "completed"} onClick={() => selectStatus("completed")} />
        <Metric icon={<FiAlertCircle />} label="异常" value={summary.data?.counts.failure} tone="failure" active={status === "failure"} onClick={() => selectStatus("failure")} />
      </section>
      {summary.error && <div className="x-task-action-error">{summary.error.message}</div>}

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
              onClick={() => selectStatus(filter.value)}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </section>

      {status === "failure" && (
        <section className="x-task-failure-action" aria-live="polite">
          <div>
            <strong>{summary.data?.counts.failure ?? 0} 条失败记录</strong>
          </div>
          <div className="x-task-action-buttons">
            <button
              type="button"
              className="x-task-command is-danger"
              disabled={clearHistory.isPending}
              onClick={requestHistoryClear}
            >
              <FiTrash2 />
              <span>{clearHistory.isPending ? "清除中" : "清除记录"}</span>
            </button>
            <button
              type="button"
              className="x-task-command"
              disabled={retryFailures.isPending || (summary.data?.counts.failure ?? 0) === 0}
              onClick={() => retryFailures.mutate()}
            >
              <FiRefreshCw className={retryFailures.isPending ? "is-spinning" : ""} />
              <span>{retryFailures.isPending ? "提交中" : "一键重试"}</span>
            </button>
          </div>
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
      {canClearHistory && status !== "failure" && (
        <section className="x-task-failure-action" aria-live="polite">
          <div>
            <strong>{historyCount} 条{historyLabel}</strong>
          </div>
          <button
            type="button"
            className="x-task-command is-danger"
            disabled={clearHistory.isPending || historyCount === 0}
            onClick={requestHistoryClear}
          >
            <FiTrash2 />
            <span>{clearHistory.isPending ? "清除中" : status === null ? "清除已结束" : "清除记录"}</span>
          </button>
        </section>
      )}
      {canClearHistory && clearHistory.data && (
        <div className="x-task-action-success">已清除 {clearHistory.data.deleted} 条任务历史</div>
      )}
      {canClearHistory && clearHistory.error && (
        <div className="x-task-action-error">{clearHistory.error.message}</div>
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
      return status === "queued" || status === "in_progress" || status === "retry_scheduled" ? 2_000 : false;
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

function Metric({ icon, label, value, tone, active, onClick }: { icon: ReactNode; label: string; value?: number; tone: string; active: boolean; onClick: () => void }) {
  return <button type="button" className={`x-task-metric is-${tone} ${active ? "is-active" : ""}`} onClick={onClick}><span>{icon}</span><div><strong>{value ?? "-"}</strong><small>{label}</small></div></button>;
}

function terminalTaskStatus(status: TaskStatus | null): TerminalTaskStatus | undefined {
  return status === "completed" || status === "failure" || status === "abandoned"
    ? status
    : undefined;
}

function terminalTaskCount(counts: { all: number; in_progress: number; queued: number; retry_scheduled: number } | undefined) {
  if (!counts) return 0;
  return Math.max(0, counts.all - counts.in_progress - counts.queued - counts.retry_scheduled);
}

function TaskRow({ task }: { task: TaskRecord }) {
  const status = STATUS_META[task.status] ?? STATUS_META.unknown;
  return (
    <Link to="/tasks/$taskId" params={{ taskId: task.id }} className="x-task-row">
      <span className="x-task-name"><strong>{taskLabel(task.name)}</strong><small>{taskSummary(task)}</small></span>
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
      <TaskSubject task={task} />
      <dl className="x-task-facts">
        <div><dt>任务 ID</dt><dd><code>{task.id}</code></dd></div>
        <div><dt>执行队列</dt><dd>{queueLabel(task.worker)}</dd></div>
        <div><dt>当前尝试</dt><dd>{task.current_attempt} / {task.max_attempts}</dd></div>
        {task.trigger && <div><dt>触发方式</dt><dd>{triggerLabel(task.trigger)}</dd></div>}
        {task.parent_task_id && <div><dt>所属同步任务</dt><dd><Link to="/tasks/$taskId" params={{ taskId: task.parent_task_id }}><code>{task.parent_task_id}</code></Link></dd></div>}
        {task.retry_of && <div><dt>重试来源</dt><dd><Link to="/tasks/$taskId" params={{ taskId: task.retry_of }}><code>{task.retry_of}</code></Link></dd></div>}
        {task.child_counts && task.child_counts.all > 0 && (
          <div><dt>派生媒体任务</dt><dd>{childCountSummary(task.child_counts)}</dd></div>
        )}
        <div><dt>进入队列</dt><dd>{formatDate(task.queued_at, true)}</dd></div>
        <div><dt>开始时间</dt><dd>{formatDate(task.started_at, true)}</dd></div>
        <div><dt>完成时间</dt><dd>{formatDate(task.finished_at, true)}</dd></div>
        {task.next_retry_at && <div><dt>下次重试</dt><dd>{formatDate(task.next_retry_at, true)}</dd></div>}
        <div><dt>执行耗时</dt><dd>{formatDuration(task.duration_ms)}</dd></div>
      </dl>
      {task.error && <DetailBlock title="错误" value={task.error} error />}
      {task.attempts && task.attempts.length > 0 && (
        <section className="x-task-attempts">
          <h3>执行尝试</h3>
          {task.attempts.map((attempt) => (
            <div className="x-task-attempt" key={attempt.attempt}>
              <div>
                <strong>第 {attempt.attempt} 次</strong>
                <StatusBadge taskStatus={attempt.status} />
              </div>
              <span>{formatDate(attempt.started_at ?? attempt.queued_at, true)}</span>
              <span>{formatDuration(attempt.duration_ms)}</span>
              {attempt.next_retry_at && <span>下次 {formatDate(attempt.next_retry_at, true)}</span>}
              {attempt.error && <pre>{attempt.error}</pre>}
            </div>
          ))}
        </section>
      )}
      <DetailBlock title="参数" value={{ args: task.args, kwargs: task.kwargs }} />
      {task.result !== null && <DetailBlock title="执行结果" value={task.result} />}
    </div>
  );
}

function TaskSubject({ task }: { task: TaskRecord }) {
  const { account, media, post } = task.context;
  if (!account && !media) return null;

  return (
    <section className="x-task-subject">
      <span className="x-task-subject-icon">{media ? <FiImage /> : <FiUser />}</span>
      <div className="x-task-subject-main">
        <span>{media ? mediaTypeLabel(media.media_type) : "同步账号"}</span>
        {account && (
          <Link to="/accounts/$xUserId" params={{ xUserId: account.x_user_id }}>
            <strong>{account.display_name || account.username || account.x_user_id}</strong>
            {account.username && <small>@{account.username}</small>}
          </Link>
        )}
        {media && <code>{media.id}</code>}
      </div>
      <div className="x-task-subject-links">
        {post && (
          <a href={post.permalink} target="_blank" rel="noreferrer">
            <FiExternalLink /><span>帖子 {post.tweet_id}</span>
          </a>
        )}
        {media?.source_url && (
          <a href={media.source_url} target="_blank" rel="noreferrer">
            <FiExternalLink /><span>媒体源</span>
          </a>
        )}
        {task.parent_task_id && (
          <Link to="/tasks/$taskId" params={{ taskId: task.parent_task_id }}>
            <FiGitBranch /><span>同步任务</span>
          </Link>
        )}
      </div>
      {post?.text_preview && <p>{post.text_preview}</p>}
    </section>
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
function taskSummary(task: TaskRecord) {
  const { account, media, post } = task.context;
  const accountLabel = account?.username ? `@${account.username}` : account?.display_name || account?.x_user_id;
  if (media) {
    return [mediaTypeLabel(media.media_type), post ? `帖子 ${post.tweet_id}` : null, accountLabel]
      .filter(Boolean)
      .join(" · ");
  }
  if (accountLabel) return [accountLabel, account?.x_user_id].filter((value, index, values) => value && values.indexOf(value) === index).join(" · ");
  if (task.name === "archivex.schedule_enabled_accounts") return scheduleResultSummary(task.result) ?? shortId(task.id);
  return shortId(task.id);
}
function scheduleResultSummary(result: unknown) {
  if (!result || typeof result !== "object") return null;
  const values = result as Record<string, unknown>;
  const queued = typeof values.queued === "number" ? values.queued : 0;
  const duplicates = typeof values.duplicates === "number" ? values.duplicates : 0;
  const skipped = typeof values.skipped === "number" ? values.skipped : 0;
  if (skipped > 0) return "未到同步时间，本轮未分发";
  if (queued === 0 && duplicates === 0) return "没有可分发的归档账号";
  return [`已分发 ${queued} 个账号`, duplicates ? `${duplicates} 个任务已存在` : null].filter(Boolean).join(" · ");
}
function mediaTypeLabel(mediaType?: string) {
  if (mediaType === "image") return "图片下载";
  if (mediaType === "video") return "视频下载";
  if (mediaType === "gif") return "动图下载";
  return "媒体下载";
}
function triggerLabel(trigger: string) {
  const labels: Record<string, string> = {
    manual: "手动同步",
    account_added: "新增账号",
    scheduled: "定时同步",
    schedule_manual: "手动运行计划",
    rerun: "重新执行",
    failure_retry: "失败重试"
  };
  return labels[trigger] ?? trigger;
}
function childCountSummary(counts: { all: number; completed: number; failure: number; in_progress: number; queued: number; retry_scheduled: number; abandoned: number }) {
  const active = counts.in_progress + counts.queued + counts.retry_scheduled;
  return [`共 ${counts.all} 条`, `完成 ${counts.completed}`, active ? `进行中 ${active}` : null, counts.failure ? `失败 ${counts.failure}` : null, counts.abandoned ? `中断 ${counts.abandoned}` : null]
    .filter(Boolean)
    .join(" · ");
}
function canRerunTask(task: TaskRecord) {
  const rerunnable = task.name === "archivex.sync_account" || task.name === "archivex.download_media";
  return rerunnable && !["queued", "in_progress", "retry_scheduled"].includes(task.status);
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
