import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { FormEvent, useEffect, useState } from "react";
import { FiSearch, FiX } from "react-icons/fi";

import { PostTimeline } from "../timeline/post-timeline";
import { getAccounts } from "../../lib/api/accounts";
import "./search-page.css";

export function SearchPage() {
  const { q: searchQuery = "", account: accountId } = useSearch({ from: "/search" });
  const navigate = useNavigate();
  const [draft, setDraft] = useState(searchQuery);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: getAccounts });
  const account = accounts.data?.find((item) => item.x_user_id === accountId);
  useEffect(() => setDraft(searchQuery), [searchQuery]);

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    void navigate({ to: "/search", search: { q: draft.trim() || undefined, account: accountId } });
  }

  function clearSearch() {
    setDraft("");
    void navigate({ to: "/search", search: { account: accountId } });
  }

  return (
    <div className="x-search-page">
      <form className="x-search-form" role="search" onSubmit={submitSearch}>
        <div className="x-search-field">
          <FiSearch aria-hidden="true" />
          <input
            autoFocus
            type="search"
            maxLength={500}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={accountId ? "搜索该账号归档的帖子" : "搜索归档帖子"}
            aria-label="搜索归档帖子"
          />
          {draft && (
            <button type="button" onClick={clearSearch} aria-label="清空搜索" title="清空">
              <FiX />
            </button>
          )}
        </div>
        <button
          className="x-search-submit"
          type="submit"
          disabled={!draft.trim()}
          aria-label="搜索"
          title="搜索"
        >
          <FiSearch />
        </button>
      </form>

      {accountId && <div className="x-scope-links" aria-label="搜索范围">
        <span>搜索范围：<Link to="/accounts/$xUserId" params={{ xUserId: accountId }}>{account?.display_name ?? account?.current_username ?? `账号 ${accountId}`}</Link></span>
        <Link to="/search" search={{ q: searchQuery || undefined }}>搜索全部账号</Link>
      </div>}

      {searchQuery && (
        <PostTimeline
          tab="posts"
          searchQuery={searchQuery}
          xUserId={accountId}
          includeReplies
          emptyMessage={`没有找到与“${searchQuery}”相关的归档帖子。`}
        />
      )}
    </div>
  );
}
