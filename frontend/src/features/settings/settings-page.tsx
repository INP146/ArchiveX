import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { FiCheck, FiEdit3, FiKey, FiServer, FiTrash2, FiX } from "react-icons/fi";

import {
  CrawlerAccount,
  getCrawlerAccounts,
  importCrawlerAccountCookies,
  setCrawlerAccountProxy
} from "../../lib/api/crawler-accounts";
import { formatCount } from "../../lib/format-number";
import "./settings-page.css";

export function SettingsPage() {
  const accounts = useQuery({
    queryKey: ["crawler-accounts"],
    queryFn: getCrawlerAccounts
  });

  return (
    <div className="x-settings-page">
      <header className="x-settings-header">
        <h1>采集设置</h1>
        <span>twscrape</span>
      </header>
      <section className="x-crawler-section" aria-labelledby="crawler-accounts-heading">
        <div className="x-settings-section-heading">
          <div>
            <h2 id="crawler-accounts-heading">采集账号</h2>
            <span>{accounts.data ? `${accounts.data.length} 个账号` : "登录会话"}</span>
          </div>
          <FiServer aria-hidden="true" />
        </div>
        {accounts.isPending && <div className="x-settings-state">正在读取采集账号...</div>}
        {accounts.error && <div className="x-settings-state is-error">{accounts.error.message}</div>}
        {accounts.data?.length === 0 && (
          <div className="x-settings-state">尚未配置采集账号。</div>
        )}
        {accounts.data?.map((account) => (
          <CrawlerAccountRow key={account.username} account={account} />
        ))}
      </section>
      <SessionImportForm onImported={() => accounts.refetch()} />
    </div>
  );
}

function SessionImportForm({ onImported }: { onImported: () => Promise<unknown> }) {
  const [username, setUsername] = useState("");
  const [cookies, setCookies] = useState("");
  const [replace, setReplace] = useState(false);
  const importSession = useMutation({
    mutationFn: () => importCrawlerAccountCookies(username.trim(), cookies.trim(), replace),
    onSuccess: async () => {
      await onImported();
      setUsername("");
      setCookies("");
      setReplace(false);
    }
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (username.trim() && cookies.trim()) importSession.mutate();
  }

  return (
    <section className="x-session-import-section" aria-labelledby="session-import-heading">
      <div className="x-settings-section-heading">
        <div>
          <h2 id="session-import-heading">添加采集账号</h2>
          <span>使用 X 浏览器 Cookie 登录</span>
        </div>
        <FiKey aria-hidden="true" />
      </div>
      <form className="x-session-import-form" onSubmit={submit}>
        <label htmlFor="crawler-username">X 用户名</label>
        <input
          id="crawler-username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          placeholder="例如 archivex_login"
          autoComplete="off"
          required
        />
        <label htmlFor="crawler-cookies">Cookie 字符串</label>
        <textarea
          id="crawler-cookies"
          value={cookies}
          onChange={(event) => setCookies(event.target.value)}
          placeholder="auth_token=...; ct0=..."
          autoComplete="off"
          spellCheck={false}
          rows={3}
          required
        />
        <label className="x-session-replace">
          <input
            type="checkbox"
            checked={replace}
            onChange={(event) => setReplace(event.target.checked)}
          />
          替换已有同名会话
        </label>
        <button type="submit" disabled={!username.trim() || !cookies.trim() || importSession.isPending}>
          {importSession.isPending ? "保存中..." : "保存采集账号"}
        </button>
        {importSession.error && <p className="x-session-import-error">{importSession.error.message}</p>}
        {importSession.isSuccess && <p className="x-session-import-success">采集账号已保存。</p>}
      </form>
    </section>
  );
}

function CrawlerAccountRow({ account }: { account: CrawlerAccount }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [proxy, setProxy] = useState("");
  const update = useMutation({
    mutationFn: (value: string | null) => setCrawlerAccountProxy(account.username, value),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["crawler-accounts"] });
      setEditing(false);
      setProxy("");
    }
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (proxy.trim()) update.mutate(proxy.trim());
  }

  function closeEditor() {
    setEditing(false);
    setProxy("");
    update.reset();
  }

  return (
    <article className="x-crawler-account-row">
      <div className="x-crawler-account-main">
        <span className={`x-crawler-status ${account.active ? "is-active" : ""}`}>
          {account.active ? <FiCheck /> : <FiX />}
        </span>
        <div className="x-crawler-account-copy">
          <strong>@{account.username}</strong>
          <span>
            {account.active ? "可用" : "不可用"}
            <span aria-hidden="true"> · </span>
            {formatCount(account.total_requests)} 次请求
          </span>
          <code>{account.proxy_url ?? "直连"}</code>
        </div>
        <button
          type="button"
          className="x-crawler-edit"
          onClick={() => editing ? closeEditor() : setEditing(true)}
          aria-label={editing ? "关闭代理编辑" : account.proxy_configured ? "更换代理" : "添加代理"}
          title={editing ? "关闭" : account.proxy_configured ? "更换代理" : "添加代理"}
        >
          {editing ? <FiX /> : <FiEdit3 />}
        </button>
      </div>
      {editing && (
        <form className="x-proxy-form" onSubmit={submit}>
          <label htmlFor={`proxy-${account.username}`}>HTTP 代理</label>
          <div className="x-proxy-input-row">
            <input
              id={`proxy-${account.username}`}
              type="password"
              autoFocus
              autoComplete="off"
              value={proxy}
              onChange={(event) => setProxy(event.target.value)}
              placeholder="http://user:pass@host:port"
            />
            <button type="submit" disabled={!proxy.trim() || update.isPending}>保存</button>
            {account.proxy_configured && (
              <button
                type="button"
                className="x-proxy-clear"
                onClick={() => update.mutate(null)}
                disabled={update.isPending}
                aria-label="清除代理"
                title="清除代理"
              >
                <FiTrash2 />
              </button>
            )}
          </div>
          {update.error && <p>{update.error.message}</p>}
        </form>
      )}
    </article>
  );
}
