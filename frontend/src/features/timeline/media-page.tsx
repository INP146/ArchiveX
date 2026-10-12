import { Link } from "@tanstack/react-router";
import { FiUsers } from "react-icons/fi";

import { PostTimeline } from "./post-timeline";

export function MediaPage() {
  return (
    <div className="x-profile-column">
      <header className="x-profile-header">
        <div className="x-header-copy x-header-copy-flush"><strong>媒体库</strong><span>全部归档账号的媒体帖子</span></div>
        <Link to="/accounts" className="x-icon-button x-header-action" aria-label="按账号浏览媒体" title="按账号浏览媒体"><FiUsers /></Link>
      </header>
      <PostTimeline tab="media" emptyMessage="还没有归档包含媒体的帖子。" />
    </div>
  );
}
