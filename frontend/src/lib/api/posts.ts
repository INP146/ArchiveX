import { apiFetch } from "./client";

export interface PostMedia {
  id: string;
  media_type: "image" | "video" | "gif" | string;
  download_status: string;
  sha256: string | null;
  error: string | null;
  url: string | null;
}

export interface ArchivedPost {
  tweet_id: string;
  account_id: number;
  username: string;
  post_type: "original" | "reply" | "repost" | "quote" | string;
  text: string;
  posted_at: string;
  permalink: string;
  first_seen_at: string;
  updated_at: string;
  media_count: number;
  reply_count: number | null;
  repost_count: number | null;
  like_count: number | null;
  view_count: number | null;
  display_text: string;
  author_display_name: string | null;
  author_username: string | null;
  author_profile_image_url: string | null;
  author_verified: boolean;
  reposted_by_display_name: string | null;
  language: string | null;
  is_translatable: boolean;
  is_ai_generated: boolean;
  media: PostMedia[];
}

export function getAccountPosts(accountId: string) {
  const query = new URLSearchParams({ account_id: accountId, limit: "50" });
  return apiFetch<ArchivedPost[]>(`/api/posts?${query}`);
}
