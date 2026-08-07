import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import {
  FiArrowLeft,
  FiCalendar,
  FiMapPin,
  FiMoreHorizontal,
  FiShare,
  FiUser
} from "react-icons/fi";

import { getAccount } from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import { AccountTimelineTab } from "../../lib/api/posts";
import { PostTimeline } from "../timeline/post-timeline";
import "./account-page.css";

export function AccountPage() {
  const { accountId } = useParams({ from: "/accounts/$accountId" });
  const [activeTab, setActiveTab] = useState<AccountTimelineTab>(() => window.location.hash === "#media" ? "media" : "posts");
  useEffect(() => {
    const syncTabFromHash = () => setActiveTab(window.location.hash === "#media" ? "media" : "posts");
    window.addEventListener("hashchange", syncTabFromHash);
    return () => window.removeEventListener("hashchange", syncTabFromHash);
  }, []);
  const account = useQuery({
    queryKey: ["account", accountId],
    queryFn: () => getAccount(accountId)
  });

  if (account.isPending) return <PageState message="正在读取账号归档..." />;
  if (account.error) return <PageError error={account.error} />;

  const profile = account.data;
  return (
    <div className="x-profile-column">
        <header className="x-profile-header">
          <Link to="/accounts" className="x-icon-button" aria-label="返回归档账号列表"><FiArrowLeft /></Link>
          <div className="x-header-copy">
            <strong>{profile.display_name ?? profile.username}</strong>
            <span>{formatCount(profile.post_count)} 帖子</span>
          </div>
        </header>

        <section className="x-profile-hero">
          <div className="x-banner">
            {profile.profile_banner_url && <img src={profile.profile_banner_url} alt="" />}
          </div>
          <div className="x-profile-details">
            <div className="x-avatar-row">
              <div className="x-avatar x-avatar-large">
                {profile.profile_image_url
                  ? <img src={profile.profile_image_url} alt={`${profile.username} 的头像`} />
                  : <FiUser aria-hidden="true" />}
              </div>
              <div className="x-profile-actions">
                <button className="x-icon-button x-outline-button" type="button" aria-label="更多操作"><FiMoreHorizontal /></button>
                <a className="x-icon-button x-outline-button" href={`/api/accounts/${profile.id}`} target="_blank" rel="noreferrer" aria-label="导出当前账号资料"><FiShare /></a>
                <span className="x-archive-status">已归档</span>
              </div>
            </div>

            <div className="x-identity">
              <h1>{profile.display_name ?? profile.username}</h1>
              <p>@{profile.username}</p>
            </div>
            {profile.description && <p className="x-bio">{profile.description}</p>}
            <div className="x-profile-meta">
              {profile.location && <span><FiMapPin />{profile.location}</span>}
              {profile.joined_at && <span><FiCalendar />{formatJoined(profile.joined_at)} 加入</span>}
            </div>
            <div className="x-follow-counts">
              <span><strong>{formatCount(profile.following_count)}</strong> 正在关注</span>
              <span><strong>{formatCount(profile.followers_count)}</strong> 关注者</span>
            </div>
          </div>
        </section>

        <nav className="x-tabs" aria-label="账号内容">
          <TabButton active={activeTab === "posts"} onClick={() => setActiveTab("posts")}>帖子</TabButton>
          <TabButton active={activeTab === "replies"} onClick={() => setActiveTab("replies")}>回复</TabButton>
          <TabButton active={activeTab === "media"} onClick={() => setActiveTab("media")}>媒体</TabButton>
        </nav>

        <PostTimeline accountId={accountId} tab={activeTab} />
    </div>
  );
}

function TabButton({ active, children, onClick }: { active: boolean; children: string; onClick: () => void }) {
  return <button type="button" className={active ? "is-active" : ""} onClick={onClick}><span>{children}</span></button>;
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

function formatCount(value: number | null) {
  if (value === null) return "0";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatJoined(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long" }).format(new Date(value));
}
