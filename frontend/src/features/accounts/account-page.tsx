import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import {
  FiArrowLeft,
  FiBarChart2,
  FiBookmark,
  FiCalendar,
  FiExternalLink,
  FiHeart,
  FiMapPin,
  FiMessageCircle,
  FiMoreHorizontal,
  FiRepeat,
  FiShare,
  FiUser
} from "react-icons/fi";

import { getAccount } from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import { ArchivedPost, getAccountPosts, PostMedia } from "../../lib/api/posts";
import "./account-page.css";

type TimelineTab = "posts" | "replies" | "media";

export function AccountPage() {
  const { accountId } = useParams({ from: "/accounts/$accountId" });
  const [activeTab, setActiveTab] = useState<TimelineTab>("posts");
  const account = useQuery({
    queryKey: ["account", accountId],
    queryFn: () => getAccount(accountId)
  });
  const posts = useQuery({
    queryKey: ["posts", accountId],
    queryFn: () => getAccountPosts(accountId)
  });
  const visiblePosts = useMemo(() => {
    const items = posts.data ?? [];
    if (activeTab === "replies") return items.filter((post) => post.post_type === "reply");
    if (activeTab === "media") return items.filter((post) => post.media.length > 0);
    return items;
  }, [activeTab, posts.data]);

  if (account.isPending) return <PageState message="正在读取账号归档..." />;
  if (account.error) return <PageError error={account.error} />;

  const profile = account.data;
  return (
    <div className="x-profile-shell">
      <aside className="x-empty-rail x-empty-rail-left" aria-hidden="true" />
      <main className="x-profile-column">
        <header className="x-profile-header">
          <Link to="/" className="x-icon-button" aria-label="返回归档总览"><FiArrowLeft /></Link>
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

        <section aria-live="polite">
          {posts.isPending && <div className="x-timeline-state">正在读取帖子...</div>}
          {posts.error && <div className="x-timeline-state">帖子加载失败：{posts.error.message}</div>}
          {!posts.isPending && !posts.error && visiblePosts.length === 0 && (
            <div className="x-timeline-state">这个分类下还没有归档内容。</div>
          )}
          {visiblePosts.map((post) => <PostItem key={post.tweet_id} post={post} avatarUrl={profile.profile_image_url} displayName={profile.display_name ?? profile.username} />)}
        </section>
      </main>
      <aside className="x-empty-rail x-empty-rail-right" aria-hidden="true" />
    </div>
  );
}

function TabButton({ active, children, onClick }: { active: boolean; children: string; onClick: () => void }) {
  return <button type="button" className={active ? "is-active" : ""} onClick={onClick}><span>{children}</span></button>;
}

function PostItem({ post, avatarUrl, displayName }: { post: ArchivedPost; avatarUrl: string | null; displayName: string }) {
  return (
    <article className="x-post">
      <div className="x-avatar x-avatar-post">
        {avatarUrl ? <img src={avatarUrl} alt="" /> : <FiUser aria-hidden="true" />}
      </div>
      <div className="x-post-body">
        <div className="x-post-heading">
          <div className="x-post-author">
            <strong>{displayName}</strong>
            <span>@{post.username}</span>
            <span>·</span>
            <time dateTime={post.posted_at}>{formatPostDate(post.posted_at)}</time>
          </div>
          <a href={post.permalink} target="_blank" rel="noreferrer" aria-label="打开原帖"><FiMoreHorizontal /></a>
        </div>
        {post.post_type === "reply" && <div className="x-post-context">回复帖子</div>}
        {post.post_type === "repost" && <div className="x-post-context">已转帖</div>}
        <p className="x-post-text">{post.text}</p>
        {post.media.length > 0 && <MediaGrid media={post.media} />}
        <div className="x-post-actions" aria-label="帖子数据">
          <PostMetric icon={<FiMessageCircle />} value={post.reply_count} label="回复" />
          <PostMetric icon={<FiRepeat />} value={post.repost_count} label="转帖" />
          <PostMetric icon={<FiHeart />} value={post.like_count} label="喜欢" />
          <PostMetric icon={<FiBarChart2 />} value={post.view_count} label="查看" />
          <span className="x-post-action-spacer" />
          <button type="button" aria-label="收藏"><FiBookmark /></button>
          <a href={post.permalink} target="_blank" rel="noreferrer" aria-label="打开原帖"><FiExternalLink /></a>
        </div>
      </div>
    </article>
  );
}

function MediaGrid({ media }: { media: PostMedia[] }) {
  const available = media.filter((item) => item.url).slice(0, 4);
  if (available.length === 0) return null;
  return (
    <div className={`x-media-grid x-media-${available.length}`}>
      {available.map((item) => item.media_type === "image"
        ? <img key={item.id} src={item.url!} alt="帖子归档媒体" loading="lazy" />
        : <video key={item.id} src={item.url!} controls preload="metadata" />)}
    </div>
  );
}

function PostMetric({ icon, value, label }: { icon: React.ReactNode; value: number | null; label: string }) {
  return <button type="button" aria-label={label}>{icon}{value !== null && <span>{formatCount(value)}</span>}</button>;
}

function PageState({ message }: { message: string }) {
  return <div className="x-profile-shell"><aside /><main className="x-profile-column x-page-state">{message}</main><aside /></div>;
}

function PageError({ error }: { error: Error }) {
  const unauthenticated = error instanceof ApiError && error.status === 401;
  return (
    <div className="x-profile-shell"><aside /><main className="x-profile-column x-page-state">
      <p>{unauthenticated ? "需要登录才能查看这个账号归档。" : error.message}</p>
      {unauthenticated && <Link to="/login">去登录</Link>}
    </main><aside /></div>
  );
}

function formatCount(value: number | null) {
  if (value === null) return "0";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatJoined(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long" }).format(new Date(value));
}

function formatPostDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(new Date(value));
}
