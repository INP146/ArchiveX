import { Link, useRouterState } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import {
  FiArchive,
  FiDatabase,
  FiDownload,
  FiHome,
  FiImage,
  FiLogOut,
  FiMoreHorizontal,
  FiSearch,
  FiServer,
  FiSettings,
  FiUser,
  FiUsers,
  FiX
} from "react-icons/fi";

import { Account } from "../lib/api/accounts";
import { SessionUser } from "../lib/api/auth";

export function ArchiveSidebar({
  account,
  accountCount,
  viewer,
  onLogout,
  onManageAccounts
}: {
  account?: Account;
  accountCount: number;
  viewer?: SessionUser | null;
  onLogout: () => void;
  onManageAccounts: () => void;
}) {
  const location = useRouterState({ select: (state) => state.location });
  const pathname = location.pathname;
  const normalizedHash = location.hash.replace(/^#/, "");
  const profilePath = account ? `/accounts/${account.x_user_id}` : "/accounts";
  const mediaPath = account ? `${profilePath}#media` : "/accounts";
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const homeActive = pathname === "/";
  const searchActive = pathname === "/search";
  const mediaActive = pathname.startsWith("/accounts/") && normalizedHash === "media";
  const accountsActive = pathname.startsWith("/accounts") && !mediaActive;
  const tasksActive = pathname.startsWith("/tasks");

  useEffect(() => {
    setMobileDrawerOpen(false);
  }, [pathname, normalizedHash]);

  useEffect(() => {
    if (!mobileDrawerOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileDrawerOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mobileDrawerOpen]);

  return (
    <>
      <aside className="x-left-rail">
        <div className="x-sidebar">
          <Link to="/" className="x-sidebar-logo" aria-label="ArchiveX 总览"><FiArchive /></Link>
          <nav className="x-sidebar-nav" aria-label="主导航">
            <Link to="/" className={`x-sidebar-item ${homeActive ? "is-active" : ""}`}><FiHome /><span>主页</span></Link>
            <Link to="/search" className={`x-sidebar-item ${searchActive ? "is-active" : ""}`}><FiSearch /><span>搜索</span></Link>
            <Link to="/accounts" className={`x-sidebar-item ${accountsActive ? "is-active" : ""}`}><FiUsers /><span>归档账号</span></Link>
            <a href={mediaPath} className={`x-sidebar-item ${mediaActive ? "is-active" : ""}`}><FiImage /><span>媒体库</span></a>
            <Link to="/tasks" className={`x-sidebar-item ${tasksActive ? "is-active" : ""}`}><FiServer /><span>任务中心</span></Link>
            {account
              ? <a href={`/api/accounts/${account.x_user_id}`} target="_blank" rel="noreferrer" className="x-sidebar-item"><FiDatabase /><span>数据导出</span></a>
              : <span className="x-sidebar-item is-disabled"><FiDatabase /><span>数据导出</span></span>}
            <Link to="/settings" className={`x-sidebar-item ${pathname === "/settings" ? "is-active" : ""}`}><FiSettings /><span>设置</span></Link>
            <button type="button" className="x-sidebar-item"><FiMoreHorizontal /><span>更多</span></button>
          </nav>

          {account ? (
            <a href={`/api/accounts/${account.x_user_id}`} target="_blank" rel="noreferrer" className="x-sidebar-primary">
              <FiDownload /><span>导出归档</span>
            </a>
          ) : <span className="x-sidebar-primary is-disabled"><FiDownload /><span>导出归档</span></span>}

          <div className="x-sidebar-account-wrap">
            {accountMenuOpen && (
              <div className="x-sidebar-account-menu">
                <button type="button" onClick={onManageAccounts}><FiUser /><span>管理归档账号</span></button>
                <button type="button" onClick={onLogout}><FiLogOut /><span>登出 @{viewer?.username ?? "账户"}</span></button>
              </div>
            )}
            <button
              type="button"
              className="x-sidebar-account"
              onClick={() => setAccountMenuOpen((open) => !open)}
              aria-expanded={accountMenuOpen}
            >
              <ViewerAvatar viewer={viewer} className="x-sidebar-avatar" />
              <span className="x-sidebar-account-copy">
                <strong>{viewer?.display_name ?? "未登录"}</strong>
                <span>{viewer ? `@${viewer.username}` : "登录账户"}</span>
              </span>
              <FiMoreHorizontal className="x-sidebar-account-more" />
            </button>
          </div>
        </div>
      </aside>

      <header className="x-mobile-topbar">
        <button
          type="button"
          className="x-mobile-avatar-button"
          onClick={() => setMobileDrawerOpen(true)}
          aria-label="打开侧栏"
          aria-expanded={mobileDrawerOpen}
          aria-controls="x-mobile-drawer"
        >
          <ViewerAvatar viewer={viewer} className="x-mobile-topbar-avatar" />
        </button>
        <Link to="/" className="x-mobile-topbar-logo" aria-label="ArchiveX 主页">
          <FiArchive />
        </Link>
        <span aria-hidden="true" />
      </header>

      <nav className="x-mobile-bottom-nav" aria-label="移动端主导航">
        <Link
          to="/"
          className={`x-mobile-bottom-item ${homeActive ? "is-active" : ""}`}
          aria-label="主页"
          aria-current={homeActive ? "page" : undefined}
          title="主页"
        >
          <FiHome /><span>主页</span>
        </Link>
        <Link
          to="/search"
          className={`x-mobile-bottom-item ${searchActive ? "is-active" : ""}`}
          aria-label="搜索"
          aria-current={searchActive ? "page" : undefined}
          title="搜索"
        >
          <FiSearch /><span>搜索</span>
        </Link>
        <Link
          to="/accounts"
          className={`x-mobile-bottom-item ${accountsActive ? "is-active" : ""}`}
          aria-label="归档账号"
          aria-current={accountsActive ? "page" : undefined}
          title="归档账号"
        >
          <FiUsers /><span>归档账号</span>
        </Link>
        <a
          href={mediaPath}
          className={`x-mobile-bottom-item ${mediaActive ? "is-active" : ""}`}
          aria-label="媒体库"
          aria-current={mediaActive ? "page" : undefined}
          title="媒体库"
        >
          <FiImage /><span>媒体库</span>
        </a>
        <Link
          to="/tasks"
          className={`x-mobile-bottom-item ${tasksActive ? "is-active" : ""}`}
          aria-label="任务中心"
          aria-current={tasksActive ? "page" : undefined}
          title="任务中心"
        >
          <FiServer /><span>任务中心</span>
        </Link>
      </nav>

      {mobileDrawerOpen && (
        <div className="x-mobile-drawer-layer">
          <button
            type="button"
            className="x-mobile-drawer-backdrop"
            onClick={() => setMobileDrawerOpen(false)}
            aria-label="关闭侧栏"
          />
          <aside
            id="x-mobile-drawer"
            className="x-mobile-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="ArchiveX 侧栏"
          >
            <div className="x-mobile-drawer-head">
              <ViewerAvatar viewer={viewer} className="x-mobile-drawer-avatar" />
              <button
                autoFocus
                type="button"
                className="x-icon-button"
                onClick={() => setMobileDrawerOpen(false)}
                aria-label="关闭侧栏"
                title="关闭"
              >
                <FiX />
              </button>
            </div>
            <div className="x-mobile-drawer-identity">
              <strong>{viewer?.display_name ?? "未登录"}</strong>
              <span>{viewer ? `@${viewer.username}` : "登录账户"}</span>
              <small>{accountCount} 个归档账号</small>
            </div>
            <nav className="x-mobile-drawer-nav" aria-label="侧栏导航">
              <Link to="/"><FiHome /><span>主页</span></Link>
              <Link to="/search"><FiSearch /><span>搜索</span></Link>
              <Link to="/accounts"><FiUsers /><span>归档账号</span></Link>
              <a href={mediaPath}><FiImage /><span>媒体库</span></a>
              <Link to="/tasks"><FiServer /><span>任务中心</span></Link>
              {account
                ? <a href={`/api/accounts/${account.x_user_id}`} target="_blank" rel="noreferrer"><FiDownload /><span>数据导出</span></a>
                : <span className="is-disabled"><FiDownload /><span>数据导出</span></span>}
              <Link to="/settings"><FiSettings /><span>设置</span></Link>
            </nav>
            <button type="button" className="x-mobile-drawer-logout" onClick={onLogout}>
              <FiLogOut /><span>登出 @{viewer?.username ?? "账户"}</span>
            </button>
          </aside>
        </div>
      )}
    </>
  );
}

function ViewerAvatar({ viewer, className }: { viewer?: SessionUser | null; className: string }) {
  return (
    <span className={`x-avatar ${className}`}>
      {viewer?.avatar_url ? <img src={viewer.avatar_url} alt="" /> : <FiUser aria-hidden="true" />}
    </span>
  );
}
