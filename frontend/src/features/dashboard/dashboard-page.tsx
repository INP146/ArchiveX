import { PostTimeline } from "../timeline/post-timeline";
import "./dashboard-page.css";

export function DashboardPage() {
  return (
    <div className="x-profile-column x-home-column">
      <nav className="x-tabs x-home-tabs" aria-label="主页内容">
        <button type="button" className="is-active" aria-current="page">
          <span>为你推荐</span>
        </button>
      </nav>
      <PostTimeline tab="posts" emptyMessage="还没有可推荐的归档内容。" />
    </div>
  );
}
