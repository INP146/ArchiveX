import { FormEvent, useState } from "react";
import { FiSearch, FiX } from "react-icons/fi";

import { PostTimeline } from "../timeline/post-timeline";
import "./search-page.css";

export function SearchPage() {
  const [draft, setDraft] = useState("");
  const [searchQuery, setSearchQuery] = useState("");

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setSearchQuery(draft.trim());
  }

  function clearSearch() {
    setDraft("");
    setSearchQuery("");
  }

  return (
    <div className="x-search-page">
      <form className="x-search-form" role="search" onSubmit={submitSearch}>
        <div className="x-search-field">
          <FiSearch aria-hidden="true" />
          <input
            autoFocus
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="搜索归档帖子"
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

      {searchQuery && (
        <PostTimeline
          tab="posts"
          searchQuery={searchQuery}
          includeReplies
          emptyMessage={`没有找到与“${searchQuery}”相关的归档帖子。`}
        />
      )}
    </div>
  );
}
