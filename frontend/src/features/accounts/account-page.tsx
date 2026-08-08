import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { BsPatchCheckFill } from "react-icons/bs";
import {
  FiArrowLeft,
  FiCalendar,
  FiMapPin,
  FiPause,
  FiPlay,
  FiRefreshCw,
  FiShare,
  FiUser
} from "react-icons/fi";

import {
  getAccount,
  getUsernameHistory,
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
  const [activeTab, setActiveTab] = useState<AccountTimelineTab>(() => (
    window.location.hash === "#media" ? "media" : "posts"
  ));
  const [failedBannerUrl, setFailedBannerUrl] = useState<string | null>(null);
  useEffect(() => {
    const syncTabFromHash = () => setActiveTab(
      window.location.hash === "#media" ? "media" : "posts"
    );
    window.addEventListener("hashchange", syncTabFromHash);
    return () => window.removeEventListener("hashchange", syncTabFromHash);
  }, []);
  const account = useQuery({
    queryKey: ["account", xUserId],
    queryFn: () => getAccount(xUserId)
  });
  const history = useQuery({
    queryKey: ["username-history", xUserId],
    queryFn: () => getUsernameHistory(xUserId)
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
                disabled={sync.isPending}
                onClick={() => sync.mutate()}
              >
                <FiRefreshCw className={sync.isPending ? "is-spinning" : ""} />
              </button>
              <a
                className="x-icon-button x-outline-button"
                href={`/api/accounts/${profile.x_user_id}`}
                target="_blank"
                rel="noreferrer"
                aria-label="导出当前账号资料"
                title="导出资料"
              >
                <FiShare />
              </a>
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
          {(sync.error || toggleArchive.error) && (
            <p className="x-profile-action-error">
              {(sync.error ?? toggleArchive.error)?.message}
            </p>
          )}
        </div>
      </section>

      <nav className="x-tabs" aria-label="账号内容">
        <TabButton active={activeTab === "posts"} onClick={() => setActiveTab("posts")}>帖子</TabButton>
        <TabButton active={activeTab === "replies"} onClick={() => setActiveTab("replies")}>回复</TabButton>
        <TabButton active={activeTab === "media"} onClick={() => setActiveTab("media")}>媒体</TabButton>
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
