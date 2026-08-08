import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef } from "react";
import { BsPatchCheckFill } from "react-icons/bs";
import {
  FiBarChart2,
  FiBookmark,
  FiHeart,
  FiMessageCircle,
  FiMoreHorizontal,
  FiRepeat,
  FiUpload,
  FiUser
} from "react-icons/fi";
import { HiOutlineTranslate } from "react-icons/hi";
import { TbSparkles } from "react-icons/tb";

import { ApiError } from "../../lib/api/client";
import {
  AccountTimelineTab,
  ArchivedPost,
  getTimelinePosts,
  PostMedia,
  POSTS_PAGE_SIZE
} from "../../lib/api/posts";
import "../accounts/account-page.css";

export function PostTimeline({
  xUserId,
  tab,
  emptyMessage = "这个分类下还没有归档内容。"
}: {
  xUserId?: string;
  tab: AccountTimelineTab;
  emptyMessage?: string;
}) {
  const timelineEndRef = useRef<HTMLDivElement>(null);
  const posts = useInfiniteQuery({
    queryKey: ["posts", xUserId ?? "all", tab],
    queryFn: ({ pageParam }) => getTimelinePosts(tab, pageParam, xUserId),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => lastPage.length === POSTS_PAGE_SIZE
      ? pages.reduce((total, page) => total + page.length, 0)
      : undefined
  });
  const timelinePosts = useMemo(() => {
    const uniquePosts = new Map<string, ArchivedPost>();
    posts.data?.pages.forEach((page) => {
      page.forEach((post) => uniquePosts.set(post.tweet_id, post));
    });
    return [...uniquePosts.values()];
  }, [posts.data]);
  const { fetchNextPage, hasNextPage, isFetchingNextPage, isFetchNextPageError } = posts;

  useEffect(() => {
    const timelineEnd = timelineEndRef.current;
    if (!timelineEnd || !hasNextPage || isFetchingNextPage || isFetchNextPageError) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) void fetchNextPage();
    }, { rootMargin: "400px 0px" });
    observer.observe(timelineEnd);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage, isFetchNextPageError]);

  return (
    <section aria-live="polite">
      {posts.isPending && <div className="x-timeline-state">正在读取帖子...</div>}
      {posts.isError && !posts.isFetchNextPageError && <TimelineError error={posts.error} />}
      {!posts.isPending && !posts.isError && timelinePosts.length === 0 && (
        <div className="x-timeline-state">{emptyMessage}</div>
      )}
      {timelinePosts.map((post) => <PostItem key={post.tweet_id} post={post} />)}
      {posts.isFetchNextPageError && (
        <div className="x-timeline-state x-timeline-more">
          <span>更多帖子加载失败</span>
          <button type="button" onClick={() => void posts.fetchNextPage()}>重试</button>
        </div>
      )}
      {posts.hasNextPage && !posts.isFetchNextPageError && (
        <div ref={timelineEndRef} className="x-timeline-state x-timeline-more">
          {posts.isFetchingNextPage ? "正在读取更多帖子..." : null}
        </div>
      )}
      {!posts.hasNextPage && timelinePosts.length > 0 && (
        <div className="x-timeline-state x-timeline-more">已显示全部帖子</div>
      )}
    </section>
  );
}

function TimelineError({ error }: { error: Error }) {
  const unauthenticated = error instanceof ApiError && error.status === 401;
  return (
    <div className="x-timeline-state">
      <p>{unauthenticated ? "需要登录才能查看归档帖子。" : `帖子加载失败：${error.message}`}</p>
      {unauthenticated && <Link to="/login">去登录</Link>}
    </div>
  );
}

function PostItem({ post }: { post: ArchivedPost }) {
  const authorName = post.author_display_name ?? post.author_username ?? post.username
    ?? post.account_x_user_id;
  const authorUsername = post.author_username ?? post.username ?? post.account_x_user_id;
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

function formatCount(value: number | null) {
  if (value === null) return "0";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
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
