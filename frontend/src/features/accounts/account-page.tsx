import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { BsPatchCheckFill } from "react-icons/bs";
import {
  FiArrowLeft,
  FiBarChart2,
  FiBookmark,
  FiCalendar,
  FiHeart,
  FiMapPin,
  FiMessageCircle,
  FiMoreHorizontal,
  FiRepeat,
  FiShare,
  FiUpload,
  FiUser
} from "react-icons/fi";
import { HiOutlineTranslate } from "react-icons/hi";
import { TbSparkles } from "react-icons/tb";

import { getAccount } from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import { ArchivedPost, getAccountPosts, PostMedia } from "../../lib/api/posts";
import "./account-page.css";

type TimelineTab = "posts" | "replies" | "media";

export function AccountPage() {
  const { accountId } = useParams({ from: "/accounts/$accountId" });
  const [activeTab, setActiveTab] = useState<TimelineTab>(() => window.location.hash === "#media" ? "media" : "posts");
  useEffect(() => {
    const syncTabFromHash = () => setActiveTab(window.location.hash === "#media" ? "media" : "posts");
    window.addEventListener("hashchange", syncTabFromHash);
    return () => window.removeEventListener("hashchange", syncTabFromHash);
  }, []);
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

        <section aria-live="polite">
          {posts.isPending && <div className="x-timeline-state">正在读取帖子...</div>}
          {posts.error && <div className="x-timeline-state">帖子加载失败：{posts.error.message}</div>}
          {!posts.isPending && !posts.error && visiblePosts.length === 0 && (
            <div className="x-timeline-state">这个分类下还没有归档内容。</div>
          )}
          {visiblePosts.map((post) => <PostItem key={post.tweet_id} post={post} />)}
        </section>
    </div>
  );
}

function TabButton({ active, children, onClick }: { active: boolean; children: string; onClick: () => void }) {
  return <button type="button" className={active ? "is-active" : ""} onClick={onClick}><span>{children}</span></button>;
}

function PostItem({ post }: { post: ArchivedPost }) {
  const authorName = post.author_display_name ?? post.author_username ?? post.username;
  const authorUsername = post.author_username ?? post.username;
  const avatarUrl = post.author_profile_image_url;
  const translationLanguage = post.language && !isChineseLanguage(post.language)
    ? formatLanguage(post.language)
    : null;
  return (
    <article className={post.post_type === "reply" ? "x-post x-post-reply" : "x-post"}>
      {post.reposted_by_display_name && (
        <div className="x-post-repost">
          <FiRepeat aria-hidden="true" />
          <span>{post.reposted_by_display_name} 已转帖</span>
        </div>
      )}
      <div className="x-post-main">
        <div className="x-avatar x-avatar-post">
          {avatarUrl ? <img src={avatarUrl} alt="" /> : <FiUser aria-hidden="true" />}
        </div>
        <div className="x-post-body">
          <div className="x-post-heading">
            <div className="x-post-author">
              <strong>{authorName}</strong>
              {post.author_verified && <BsPatchCheckFill className="x-verified" aria-label="已认证" />}
              <span className="x-post-handle">@{authorUsername}</span>
              <span>·</span>
              <time dateTime={post.posted_at}>{formatPostDate(post.posted_at)}</time>
            </div>
            <div className="x-post-tools">
              {post.is_translatable && translationLanguage && (
                <a href={post.permalink} target="_blank" rel="noreferrer" aria-label="在 X 上翻译" title="翻译帖子">
                  <HiOutlineTranslate />
                </a>
              )}
              <a href={post.permalink} target="_blank" rel="noreferrer" aria-label="打开原帖菜单" title="更多">
                <FiMoreHorizontal />
              </a>
            </div>
          </div>
          {post.post_type === "reply" && (
            <div className="x-post-context">
              <span>回复</span>
              {post.reply_to_username && (
                <a href={`https://x.com/${post.reply_to_username}`} target="_blank" rel="noreferrer">
                  @{post.reply_to_username}
                </a>
              )}
            </div>
          )}
          {post.is_translatable && translationLanguage && (
            <div className="x-post-translation">
              <HiOutlineTranslate aria-hidden="true" />
              <span>翻译自 {translationLanguage}</span>
              <a href={post.permalink} target="_blank" rel="noreferrer">在 X 上翻译</a>
            </div>
          )}
          {post.display_text && <p className="x-post-text">{post.display_text}</p>}
          {post.media.length > 0 && <MediaGrid media={post.media} />}
          {post.is_ai_generated && (
            <div className="x-post-ai-label"><TbSparkles aria-hidden="true" />由 AI 生成</div>
          )}
          <div className="x-post-actions" aria-label="帖子数据">
            <PostMetric icon={<FiMessageCircle />} value={post.reply_count} label="回复" />
            <PostMetric icon={<FiRepeat />} value={post.repost_count} label="转帖" />
            <PostMetric icon={<FiHeart />} value={post.like_count} label="喜欢" />
            <PostMetric icon={<FiBarChart2 />} value={post.view_count} label="查看" />
            <button className="x-post-utility" type="button" aria-label="收藏" title="收藏"><FiBookmark /></button>
            <a className="x-post-utility" href={post.permalink} target="_blank" rel="noreferrer" aria-label="分享原帖" title="分享"><FiUpload /></a>
          </div>
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
  return <button className="x-post-metric" type="button" aria-label={label}>{icon}{value !== null && <span>{formatCount(value)}</span>}</button>;
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

function formatPostDate(value: string) {
  const date = new Date(value);
  const elapsedMs = Math.max(0, Date.now() - date.getTime());
  const elapsedMinutes = Math.floor(elapsedMs / 60_000);
  if (elapsedMinutes < 1) return "刚刚";
  if (elapsedMinutes < 60) return `${elapsedMinutes}分钟`;
  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) return `${elapsedHours}小时`;
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(date);
}

function formatLanguage(code: string) {
  try {
    return new Intl.DisplayNames(["zh-CN"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

function isChineseLanguage(code: string) {
  return code.toLowerCase() === "zh" || code.toLowerCase().startsWith("zh-");
}
