import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { BsPatchCheckFill } from "react-icons/bs";
import { FiUser } from "react-icons/fi";

import { AccountSummary, getAccounts } from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";
import "./accounts-page.css";

export function AccountsPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: getAccounts });

  return (
    <div className="x-accounts-page">
      <header className="x-accounts-header">
        <h1>归档账号</h1>
        <span>{accounts.data ? `${accounts.data.length} 个账号` : "所有账号"}</span>
      </header>

      <section className="x-account-list" aria-live="polite">
        {accounts.isPending && <div className="x-account-list-state">正在读取归档账号...</div>}
        {accounts.error && <AccountsError error={accounts.error} />}
        {accounts.data?.length === 0 && (
          <div className="x-account-list-state">尚未归档任何账号。</div>
        )}
        {accounts.data?.map((account) => <AccountRow key={account.id} account={account} />)}
      </section>
    </div>
  );
}

function AccountRow({ account }: { account: AccountSummary }) {
  const displayName = account.display_name ?? account.username;
  return (
    <Link
      to="/accounts/$accountId"
      params={{ accountId: String(account.id) }}
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
          {account.verified && <BsPatchCheckFill className="x-account-list-verified" aria-label="已认证" />}
        </span>
        <span className="x-account-list-handle">@{account.username}</span>
        <span className="x-account-list-bio">
          {account.description || `${formatCount(account.post_count)} 条帖子已归档`}
        </span>
      </span>
      <span className="x-account-list-action" aria-hidden="true">查看</span>
    </Link>
  );
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

function formatCount(value: number) {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}
