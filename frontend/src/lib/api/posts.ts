import { apiFetch } from "./client";

export interface PostMedia {
  id: string;
  media_type: string;
  download_status: string;
  sha256: string | null;
  error: string | null;
  url: string | null;
}

export interface PostAuthor {
  x_user_id: string;
  username: string | null;
  display_name: string | null;
  profile_image_url: string | null;
}

export interface PostContent {
  tweet_id: string;
  post_type: "original" | "reply" | "quote";
  text: string;
  display_text: string;
  posted_at: string;
  permalink: string;
  availability: "available" | "partial" | "deleted_or_unavailable" | "unknown";
  author: PostAuthor;
  author_verified: boolean;
  reply_to_username: string | null;
  language: string | null;
  is_translatable: boolean;
  is_ai_generated: boolean;
  reply_count: number | null;
  repost_count: number | null;
  like_count: number | null;
  view_count: number | null;
  media: PostMedia[];
  reference: PostContent | null;
}

export type TimelineItem = ({
  item_type: "post";
} & PostContent | {
  item_type: "repost";
  tweet_id: string;
  post_type: "repost";
  posted_at: string;
  reposted_at: string;
  permalink: string;
  reposter: PostAuthor;
  origin: PostContent;
}) & {
  observed_account_x_user_id: string | null;
};

export type AccountTimelineTab = "posts" | "replies" | "media";
export const POSTS_PAGE_SIZE = 50;

export function getTimelinePosts(
  tab: AccountTimelineTab,
  offset: number,
  xUserId?: string,
  searchQuery?: string,
  includeReplies = false
) {
  const query = new URLSearchParams({ limit: String(POSTS_PAGE_SIZE), offset: String(offset) });
  if (xUserId !== undefined) query.set("observed_account_x_user_id", xUserId);
  if (searchQuery) query.set("q", searchQuery);
  if (tab === "posts" && !includeReplies) query.set("exclude_post_type", "reply");
  if (tab === "replies") query.set("post_type", "reply");
  if (tab === "media") query.set("has_media", "true");
  return apiFetch<TimelineItem[]>(`/api/posts?${query}`);
}
