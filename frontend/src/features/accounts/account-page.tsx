import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useRouterState } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { BsPatchCheckFill } from "react-icons/bs";
import {
  FiArrowLeft,
  FiCalendar,
  FiExternalLink,
  FiMapPin,
  FiPause,
  FiPlay,
  FiRefreshCw,
  FiSearch,
  FiServer,
  FiUser
} from "react-icons/fi";

import {
  getAccount,
  getUsernameHistory,
  getPostFetches,
  retryPostFetches,
  setAccountEnabled,
  syncAccount
} from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import { AccountTimelineTab } from "../../lib/api/posts";
import { formatCount } from "../../lib/format-number";
import { PostTimeline } from "../timeline/post-timeline";
import "./account-page.css";

export function AccountPage() {
  const { xUserId } = useParams({ from: "/accounts/$xUserId" });
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const hash = useRouterState({ select: (state) => state.location.hash.replace(/^#/, "") });
  const activeTab: AccountTimelineTab = hash === "media" || hash === "replies" ? hash : "posts";
  const [failedBannerUrl, setFailedBannerUrl] = useState<string | null>(null);
  const account = useQuery({
    queryKey: ["account", xUserId],
    queryFn: () => getAccount(xUserId)
  });
  const history = useQuery({
    queryKey: ["username-history", xUserId],
    queryFn: () => getUsernameHistory(xUserId)
  });
  const postFetches = useQuery({
    queryKey: ["post-fetches", xUserId],
    queryFn: () => getPostFetches(xUserId),
    refetchInterval: (query) => query.state.data?.pending ? 5_000 : false
  });
  const previousGaps = useRef<{ account: string; count: number } | null>(null);
  useEffect(() => {
    if (!postFetches.data) return;
    const count = postFetches.data.pending + postFetches.data.unavailable + (postFetches.data.failed ?? 0);
    if (previousGaps.current?.account === xUserId && count < previousGaps.current.count) {
      void queryClient.invalidateQueries({ queryKey: ["posts", xUserId] });
      void queryClient.invalidateQueries({ queryKey: ["account", xUserId] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    }
    previousGaps.current = { account: xUserId, count };
  }, [postFetches.data, xUserId, queryClient]);
  const retryPosts = useMutation({
    mutationFn: () => retryPostFetches(xUserId),
    onSuccess: async () => { await queryClient.invalidateQueries(); }
  });
  const toggleArchive = useMutation({
    mutationFn: (enabled: boolean) => setAccountEnabled(xUserId, enabled),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["account", xUserId] }),
        queryClient.invalidateQueries({ queryKey: ["accounts"] })
      ]);
    }
  });
  const sync = useMutation({
    mutationFn: () => syncAccount(xUserId),
    onSuccess: async () => {
      await queryClient.invalidateQueries();
    }
  });
  const fullScan = useMutation({
    mutationFn: () => syncAccount(xUserId, true),
    onSuccess: async () => { await queryClient.invalidateQueries(); }
  });

  function selectTab(tab: AccountTimelineTab) {
    void navigate({ to: "/accounts/$xUserId", params: { xUserId }, hash: tab === "posts" ? "" : tab });
  }

  if (account.isPending) return <PageState message="正在读取账号归档..." />;
  if (account.error) return <PageError error={account.error} />;

  const profile = account.data;
  const username = profile.current_username;
  const displayName = profile.display_name ?? username ?? profile.x_user_id;
  const bannerUrl = profile.profile_banner_url;
  const previousUsernames = history.data?.filter((item) => item.observed_to !== null) ?? [];
  return (
    <div className="x-profile-column">
      <header className="x-profile-header">
        <Link to="/accounts" className="x-icon-button" aria-label="返回归档账号列表">
          <FiArrowLeft />
        </Link>
        <div className="x-header-copy">
          <strong>{displayName}</strong>
          <span>{formatCount(profile.post_count)} 帖子</span>
        </div>
      </header>

      <section className="x-profile-hero">
        <div className="x-banner">
          {bannerUrl && failedBannerUrl !== bannerUrl && (
            <img src={bannerUrl} alt="" onError={() => setFailedBannerUrl(bannerUrl)} />
          )}
        </div>
        <div className="x-profile-details">
          <div className="x-avatar-row">
            <div className="x-avatar x-avatar-large">
              {profile.profile_image_url
                ? <img src={profile.profile_image_url} alt={`${displayName} 的头像`} />
                : <FiUser aria-hidden="true" />}
            </div>
            <div className="x-profile-actions">
              <button
                className="x-icon-button x-outline-button"
                type="button"
                aria-label="立即同步"
                title="立即同步"
                disabled={sync.isPending || fullScan.isPending}
                onClick={() => { fullScan.reset(); sync.mutate(); }}
              >
                <FiRefreshCw className={sync.isPending ? "is-spinning" : ""} />
              </button>
              <button
                className="x-full-scan-button"
                type="button"
                title="重新扫描全部可读取的历史内容，补查遗漏；X 接口可能无法提供全部历史帖子"
                disabled={sync.isPending || fullScan.isPending}
                onClick={() => { sync.reset(); fullScan.mutate(); }}
              >
                <FiSearch aria-hidden="true" />
                <span>{fullScan.isPending ? "提交中" : "全量扫描"}</span>
              </button>
              <button
                className="x-archive-toggle"
                type="button"
                disabled={toggleArchive.isPending}
                onClick={() => toggleArchive.mutate(!profile.archive_enabled)}
              >
                {profile.archive_enabled ? <FiPause /> : <FiPlay />}
                <span>{profile.archive_enabled ? "暂停归档" : "恢复归档"}</span>
              </button>
            </div>
          </div>

          <div className="x-identity">
            <h1>
              <span>{displayName}</span>
              {profile.verified && (
                <BsPatchCheckFill className="x-profile-verified" aria-label="已认证" />
              )}
            </h1>
            <p>{username ? `@${username}` : `X ID ${profile.x_user_id}`}</p>
          </div>
          {profile.description && <p className="x-bio">{profile.description}</p>}
          <div className="x-profile-meta">
            {profile.location && <span><FiMapPin />{profile.location}</span>}
            {profile.joined_at && (
              <span><FiCalendar />{formatJoined(profile.joined_at)} 加入</span>
            )}
            <span className="x-stable-id">X ID {profile.x_user_id}</span>
          </div>
          <div className="x-follow-counts">
            <span><strong>{formatCount(profile.following_count)}</strong> 正在关注</span>
            <span><strong>{formatCount(profile.followers_count)}</strong> 关注者</span>
          </div>
          {previousUsernames.length > 0 && (
            <div className="x-username-history">
              <span>曾用</span>
              {previousUsernames.map((item) => (
                <span key={item.id}>@{item.username}</span>
              ))}
            </div>
          )}
          <nav className="x-profile-links" aria-label="账号相关页面">
            <Link to="/search" search={{ account: xUserId }}><FiSearch />搜索该账号</Link>
            <Link to="/tasks" search={{ account: xUserId }}><FiServer />查看账号任务</Link>
            <a href={username ? `https://x.com/${encodeURIComponent(username)}` : `https://x.com/i/user/${xUserId}`} target="_blank" rel="noreferrer"><FiExternalLink />打开 X 主页</a>
          </nav>
          {(sync.error || fullScan.error || toggleArchive.error) && (
            <p className="x-profile-action-error">
              {(sync.error ?? fullScan.error ?? toggleArchive.error)?.message}
            </p>
          )}
          {sync.data && (
            <p className="x-profile-action-status">
              {sync.data.duplicate ? "同步任务已在队列中" : "同步任务已加入队列"}
              {" · "}
              <Link to="/tasks/$taskId" params={{ taskId: sync.data.task_id }}>
                查看任务
              </Link>
            </p>
          )}
        </div>
      </section>

      {fullScan.data && (
        <p className="x-profile-action-status x-full-scan-status" role="status">
          {fullScan.data.duplicate
            ? "账号已有同步任务，未新建全量扫描；请在该任务结束后重试"
            : "全量扫描任务已加入队列，可在任务详情查看结果"}
          {" · "}
          <Link to="/tasks/$taskId" params={{ taskId: fullScan.data.task_id }}>查看任务</Link>
        </p>
      )}
      {postFetches.error && <p className="x-profile-action-error">待补档状态读取失败：{postFetches.error.message}</p>}
      {postFetches.data && (postFetches.data.pending > 0 || postFetches.data.unavailable > 0 || (postFetches.data.failed ?? 0) > 0) && (
        <details className="x-post-recovery" key={postFetches.data.pending > 0 ? "pending" : "history"}
          open={postFetches.data.pending > 0} aria-label="帖子补档状态">
          <summary>{postFetches.data.pending > 0
            ? `${postFetches.data.pending} 条帖子待补`
            : ["补档历史", postFetches.data.unavailable ? `${postFetches.data.unavailable} 条不可访问` : null,
              postFetches.data.failed ? `${postFetches.data.failed} 条重试已停止` : null].filter(Boolean).join(" · ")}</summary>
          <p>不可访问和重试耗尽的记录已退出自动补档，不影响其他帖子归档。需要时可以手动重新检查。</p>
          <button className="x-full-scan-button" type="button" disabled={retryPosts.isPending}
            onClick={() => retryPosts.mutate()}>{retryPosts.isPending ? "提交中" : "重试待补及不可访问帖子"}</button>
          {retryPosts.error && <p className="x-profile-action-error">{retryPosts.error.message}</p>}
          {retryPosts.data && <p role="status">已处理 {retryPosts.data.queued} 条补档请求
            {retryPosts.data.duplicates > 0 && `，${retryPosts.data.duplicates} 条已有任务`}
            {retryPosts.data.failed > 0 && `，${retryPosts.data.failed} 条提交或执行失败`}</p>}
          <details>
            <summary>查看明细（前 {postFetches.data.items.length} 条）</summary>
            <ul>{postFetches.data.items.map((item) => (
              <li key={item.tweet_id}>
                <Link to="/posts/$tweetId" params={{ tweetId: item.tweet_id }}>{item.tweet_id}</Link>
                {" · "}<a href={`https://x.com/i/status/${item.tweet_id}`} target="_blank" rel="noreferrer">打开 X 原帖</a>
                {" · "}{item.status === "unavailable" ? "不可访问" : item.retry_exhausted ? "重试已停止" : "待补"}{" · "}已尝试 {item.attempt_count} 次
                {item.last_error && <span>{item.last_error}</span>}
              </li>
            ))}</ul>
          </details>
          <Link to="/tasks" search={{ account: xUserId, q: "archivex.fetch_post_detail" }}>查看补档任务</Link>
        </details>
      )}
      <nav className="x-tabs" aria-label="账号内容">
        <TabButton active={activeTab === "posts"} onClick={() => selectTab("posts")}>帖子</TabButton>
        <TabButton active={activeTab === "replies"} onClick={() => selectTab("replies")}>回复</TabButton>
        <TabButton active={activeTab === "media"} onClick={() => selectTab("media")}>媒体</TabButton>
      </nav>

      <PostTimeline xUserId={xUserId} tab={activeTab} />
    </div>
  );
}

function TabButton({ active, children, onClick }: {
  active: boolean;
  children: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className={active ? "is-active" : ""} onClick={onClick}>
      <span>{children}</span>
    </button>
  );
}

function PageState({ message }: { message: string }) {
  return <div className="x-profile-column x-page-state">{message}</div>;
}

function PageError({ error }: { error: Error }) {
  const unauthenticated = error instanceof ApiError && error.status === 401;
  return (
    <div className="x-profile-column x-page-state">
      <p>{unauthenticated ? "需要登录才能查看这个账号归档。" : error.message}</p>
      {unauthenticated && <Link to="/login">去登录</Link>}
    </div>
  );
}

function formatJoined(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long"
  }).format(new Date(value));
}
