import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { FormEvent, useState } from "react";
import { BsPatchCheckFill } from "react-icons/bs";
import { FiCheck, FiPlus, FiSearch, FiUser, FiX } from "react-icons/fi";

import {
  AccountSummary,
  addAccount,
  getAccounts,
  resolveAccount,
  ResolvedAccount
} from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import { formatCount } from "../../lib/format-number";
import "./accounts-page.css";

export function AccountsPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [candidate, setCandidate] = useState<ResolvedAccount | null>(null);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: getAccounts });
  const resolve = useMutation({
    mutationFn: () => resolveAccount(query),
    onSuccess: setCandidate
  });
  const add = useMutation({
    mutationFn: addAccount,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      closeAddPanel();
    }
  });

  function closeAddPanel() {
    setAdding(false);
    setQuery("");
    setCandidate(null);
    resolve.reset();
    add.reset();
  }

  function submitLookup(event: FormEvent) {
    event.preventDefault();
    setCandidate(null);
    resolve.mutate();
  }

  return (
    <div className="x-accounts-page">
      <header className="x-accounts-header">
        <div>
          <h1>归档账号</h1>
          <span>{accounts.data ? `${accounts.data.length} 个账号` : "所有账号"}</span>
        </div>
        <button
          type="button"
          className="x-icon-button"
          aria-label={adding ? "关闭添加账号" : "添加归档账号"}
          title={adding ? "关闭" : "添加账号"}
          onClick={() => adding ? closeAddPanel() : setAdding(true)}
        >
          {adding ? <FiX /> : <FiPlus />}
        </button>
      </header>

      {adding && (
        <section className="x-account-add" aria-label="添加归档账号">
          <form className="x-account-lookup" onSubmit={submitLookup}>
            <FiSearch aria-hidden="true" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="输入 @username 或 X 主页链接"
              aria-label="X username 或主页链接"
            />
            <button type="submit" disabled={!query.trim() || resolve.isPending}>
              {resolve.isPending ? "查询中" : "查询"}
            </button>
          </form>
          {resolve.error && <InlineError error={resolve.error} />}
          {candidate && (
            <ResolvedAccountRow
              account={candidate}
              pending={add.isPending}
              onConfirm={() => add.mutate(candidate)}
            />
          )}
          {add.error && <InlineError error={add.error} />}
        </section>
      )}

      <section className="x-account-list" aria-live="polite">
        {accounts.isPending && <div className="x-account-list-state">正在读取归档账号...</div>}
        {accounts.error && <AccountsError error={accounts.error} />}
        {accounts.data?.length === 0 && (
          <div className="x-account-list-state">尚未归档任何账号。</div>
        )}
        {accounts.data?.map((account) => (
          <AccountRow key={account.x_user_id} account={account} />
        ))}
      </section>
    </div>
  );
}

function ResolvedAccountRow({
  account,
  pending,
  onConfirm
}: {
  account: ResolvedAccount;
  pending: boolean;
  onConfirm: () => void;
}) {
  return (
    <div className="x-resolved-account">
      <span className="x-avatar x-account-list-avatar">
        {account.profile_image_url
          ? <img src={account.profile_image_url} alt="" />
          : <FiUser aria-hidden="true" />}
      </span>
      <span className="x-account-list-copy">
        <strong>{account.display_name ?? account.current_username}</strong>
        <span className="x-account-list-handle">@{account.current_username}</span>
        <span className="x-account-id">ID {account.x_user_id}</span>
      </span>
      <button
        type="button"
        onClick={onConfirm}
        disabled={pending || account.archive_enabled === true}
      >
        <FiCheck />
        <span>{pending
          ? "添加中"
          : account.archive_enabled === true
            ? "已在归档"
            : account.already_archived ? "重新启用" : "确认添加"}</span>
      </button>
    </div>
  );
}

function AccountRow({ account }: { account: AccountSummary }) {
  const displayName = account.display_name ?? account.current_username ?? account.x_user_id;
  return (
    <Link
      to="/accounts/$xUserId"
      params={{ xUserId: account.x_user_id }}
      className="x-account-row"
      aria-label={`查看 ${displayName} 的归档主页`}
    >
      <span className="x-avatar x-account-list-avatar">
        {account.profile_image_url
          ? <img src={account.profile_image_url} alt="" />
          : <FiUser aria-hidden="true" />}
      </span>
      <span className="x-account-list-copy">
        <span className="x-account-list-name">
          <strong>{displayName}</strong>
          {account.verified && (
            <BsPatchCheckFill className="x-account-list-verified" aria-label="已认证" />
          )}
        </span>
        <span className="x-account-list-handle">
          {account.current_username ? `@${account.current_username}` : `ID ${account.x_user_id}`}
        </span>
        <span className="x-account-list-bio">
          {account.archive_enabled
            ? account.description || `${formatCount(account.post_count)} 条帖子已归档`
            : "归档已暂停"}
        </span>
      </span>
      <span className="x-account-list-action" aria-hidden="true">查看</span>
    </Link>
  );
}

function InlineError({ error }: { error: Error }) {
  return <p className="x-account-inline-error">{error.message}</p>;
}

function AccountsError({ error }: { error: Error }) {
  const unauthenticated = error instanceof ApiError && error.status === 401;
  return (
    <div className="x-account-list-state">
      <p>{unauthenticated ? "需要登录才能查看归档账号。" : error.message}</p>
      {unauthenticated && <Link to="/login">去登录</Link>}
    </div>
  );
}
