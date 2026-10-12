import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useRouter } from "@tanstack/react-router";
import { FiArrowLeft } from "react-icons/fi";

import { ApiError } from "../../lib/api/client";
import { getPost, type PostContent } from "../../lib/api/posts";

export function PostPage() {
  const { tweetId } = useParams({ from: "/posts/$tweetId" });
  const router = useRouter();
  const post = useQuery({ queryKey: ["post", tweetId], queryFn: () => getPost(tweetId) });
  const item = post.data;
  const content: PostContent | undefined = item?.item_type === "post" ? item : item?.origin;

  function goBack() {
    if (router.history.canGoBack()) router.history.back();
    else void router.navigate({ to: "/" });
  }

  return (
    <div className="x-profile-column">
      <header className="x-profile-header">
        <button type="button" className="x-icon-button" onClick={goBack} aria-label="返回上一页"><FiArrowLeft /></button>
        <div className="x-header-copy"><strong>帖子详情</strong><span>帖子 {tweetId}</span></div>
      </header>
      <section className="x-post-placeholder">
        <h1>帖子详情页待完善</h1>
        <p>完整内容与会话串后续会在这里展示。目前可以先打开 X 查看原帖。</p>
        <a href={content?.permalink ?? `https://x.com/i/status/${encodeURIComponent(tweetId)}`} target="_blank" rel="noreferrer">打开 X 原帖</a>
        {post.data?.item_type === "repost" && <Link to="/posts/$tweetId" params={{ tweetId: post.data.origin.tweet_id }}>查看转帖原帖</Link>}
        {post.data?.item_type === "repost" && <a href={post.data.permalink} target="_blank" rel="noreferrer">打开 X 转帖</a>}
        {content?.reference && <Link to="/posts/$tweetId" params={{ tweetId: content.reference.tweet_id }}>{content.post_type === "reply" ? "查看回复上文" : "查看引用帖"}</Link>}
        {post.data?.observed_account_x_user_id && <Link to="/accounts/$xUserId" params={{ xUserId: post.data.observed_account_x_user_id }}>查看归档来源账号</Link>}
      </section>
      {post.error && (
        <div className="x-timeline-state">
          <p>{post.error instanceof ApiError && post.error.status === 404
            ? "这条帖子尚未归档。" : post.error instanceof ApiError && post.error.status === 401
              ? "需要登录才能查看归档帖子。" : `帖子加载失败：${post.error.message}`}</p>
          {post.error instanceof ApiError && post.error.status === 401
            ? <Link to="/login">去登录</Link>
            : <button type="button" onClick={() => void post.refetch()}>重新读取</button>}
        </div>
      )}
    </div>
  );
}
