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
import { SessionUser } from "../lib/api/auth";

export function ArchiveSidebar({
  account,
  viewer,
  onLogout,
  onSwitchAccount
}: {
  account?: Account;
  viewer?: SessionUser | null;
  onLogout: () => void;
  onSwitchAccount: () => void;
}) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const profilePath = account ? `/accounts/${account.id}` : "/";
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);

  return (
    <aside className="x-left-rail">
      <div className="x-sidebar">
        <Link to="/" className="x-sidebar-logo" aria-label="ArchiveX 总览"><FiArchive /></Link>
        <nav className="x-sidebar-nav" aria-label="主导航">
          <Link to="/" className={`x-sidebar-item ${pathname === "/" ? "is-active" : ""}`}><FiHome /><span>主页</span></Link>
          <button type="button" className="x-sidebar-item"><FiSearch /><span>搜索</span></button>
          <Link to="/accounts" className={`x-sidebar-item ${pathname === "/accounts" ? "is-active" : ""}`}><FiUsers /><span>归档账号</span></Link>
          <a href={`${profilePath}#media`} className="x-sidebar-item"><FiImage /><span>媒体库</span></a>
          <Link to="/sync-runs" className={`x-sidebar-item ${pathname === "/sync-runs" ? "is-active" : ""}`}><FiActivity /><span>同步记录</span></Link>
          {account
            ? <a href={`/api/accounts/${account.id}`} target="_blank" rel="noreferrer" className="x-sidebar-item"><FiDatabase /><span>数据导出</span></a>
            : <span className="x-sidebar-item is-disabled"><FiDatabase /><span>数据导出</span></span>}
          <button type="button" className="x-sidebar-item"><FiSettings /><span>设置</span></button>
          <button type="button" className="x-sidebar-item"><FiMoreHorizontal /><span>更多</span></button>
        </nav>

        {account ? (
          <a href={`/api/accounts/${account.id}`} target="_blank" rel="noreferrer" className="x-sidebar-primary">
            <FiDownload /><span>导出归档</span>
          </a>
        ) : <span className="x-sidebar-primary is-disabled"><FiDownload /><span>导出归档</span></span>}

        <div className="x-sidebar-account-wrap">
          {accountMenuOpen && (
            <div className="x-sidebar-account-menu">
              <button type="button" onClick={onSwitchAccount}><FiUser /><span>添加已有账号</span></button>
              <button type="button" onClick={onLogout}><FiLogOut /><span>登出 @{viewer?.username ?? "账户"}</span></button>
            </div>
          )}
          <button
            type="button"
            className="x-sidebar-account"
            onClick={() => setAccountMenuOpen((open) => !open)}
            aria-expanded={accountMenuOpen}
          >
            <span className="x-avatar x-sidebar-avatar">
              {viewer?.avatar_url ? <img src={viewer.avatar_url} alt="" /> : <FiUser aria-hidden="true" />}
            </span>
            <span className="x-sidebar-account-copy">
              <strong>{viewer?.display_name ?? "未登录"}</strong>
              <span>{viewer ? `@${viewer.username}` : "登录账户"}</span>
            </span>
            <FiMoreHorizontal className="x-sidebar-account-more" />
          </button>
        </div>
      </div>
    </aside>
  );
}
