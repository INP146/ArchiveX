import { Link, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import {
  FiActivity,
  FiArchive,
  FiDatabase,
  FiDownload,
  FiHome,
  FiImage,
  FiLogOut,
  FiMoreHorizontal,
  FiSearch,
  FiSettings,
  FiUser,
  FiUsers
} from "react-icons/fi";

import { Account } from "../lib/api/accounts";

export function ArchiveSidebar({ account, onLogout }: { account?: Account; onLogout: () => void }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const profilePath = account ? `/accounts/${account.id}` : "/";
  const [moreOpen, setMoreOpen] = useState(false);

  return (
    <aside className="x-left-rail">
      <div className="x-sidebar">
        <Link to="/" className="x-sidebar-logo" aria-label="ArchiveX 总览"><FiArchive /></Link>
        <nav className="x-sidebar-nav" aria-label="主导航">
          <Link to="/" className={`x-sidebar-item ${pathname === "/" ? "is-active" : ""}`}><FiHome /><span>主页</span></Link>
          <button type="button" className="x-sidebar-item"><FiSearch /><span>搜索</span></button>
          <a href={profilePath} className={`x-sidebar-item ${pathname.startsWith("/accounts/") ? "is-active" : ""}`}><FiUsers /><span>归档账号</span></a>
          <a href={`${profilePath}#media`} className="x-sidebar-item"><FiImage /><span>媒体库</span></a>
          <Link to="/sync-runs" className={`x-sidebar-item ${pathname === "/sync-runs" ? "is-active" : ""}`}><FiActivity /><span>同步记录</span></Link>
          {account
            ? <a href={`/api/accounts/${account.id}`} target="_blank" rel="noreferrer" className="x-sidebar-item"><FiDatabase /><span>数据导出</span></a>
            : <span className="x-sidebar-item is-disabled"><FiDatabase /><span>数据导出</span></span>}
          <button type="button" className="x-sidebar-item"><FiSettings /><span>设置</span></button>
          <div className="x-sidebar-more-wrap">
            <button type="button" className="x-sidebar-item" onClick={() => setMoreOpen((open) => !open)} aria-expanded={moreOpen}><FiMoreHorizontal /><span>更多</span></button>
            {moreOpen && (
              <div className="x-sidebar-menu">
                <button type="button" onClick={onLogout}><FiLogOut /><span>退出登录</span></button>
              </div>
            )}
          </div>
        </nav>

        {account ? (
          <a href={`/api/accounts/${account.id}`} target="_blank" rel="noreferrer" className="x-sidebar-primary">
            <FiDownload /><span>导出归档</span>
          </a>
        ) : <span className="x-sidebar-primary is-disabled"><FiDownload /><span>导出归档</span></span>}

        <a href={profilePath} className="x-sidebar-account">
          <span className="x-avatar x-sidebar-avatar">
            {account?.profile_image_url ? <img src={account.profile_image_url} alt="" /> : <FiUser aria-hidden="true" />}
          </span>
          <span className="x-sidebar-account-copy">
            <strong>{account?.display_name ?? "ArchiveX"}</strong>
            <span>{account ? `@${account.username}` : "私人归档"}</span>
          </span>
          <FiMoreHorizontal className="x-sidebar-account-more" />
        </a>
      </div>
    </aside>
  );
}
