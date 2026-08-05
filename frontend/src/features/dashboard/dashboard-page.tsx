import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { getAccounts } from "../../lib/api/accounts";
import { ApiError } from "../../lib/api/client";

export function DashboardPage() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: getAccounts });

  return (
    <section className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">归档总览</h1>
        <p className="mt-1 text-sm text-slate-600">已归档账号与最近同步状态。</p>
      </div>
      {accounts.isPending && <p className="text-sm text-slate-600">正在读取归档数据...</p>}
      {accounts.error && <ErrorState error={accounts.error} />}
      {accounts.data && (
        <div className="overflow-hidden border border-slate-200 bg-white">
          {accounts.data.length === 0 ? (
            <p className="p-5 text-sm text-slate-600">尚未归档任何账号。</p>
          ) : accounts.data.map((account) => (
            <Link
              key={account.id}
              to="/accounts/$accountId"
              params={{ accountId: String(account.id) }}
              className="flex items-center justify-between border-b border-slate-100 p-4 last:border-0 hover:bg-slate-50"
            >
              <span><strong>{account.display_name ?? account.username}</strong><span className="ml-2 text-sm text-slate-500">@{account.username}</span></span>
              <span className="text-sm text-slate-600">{account.post_count} 条帖子</span>
            </Link>
          ))}
        </div>
      )}
    </section>
  );
}

function ErrorState({ error }: { error: Error }) {
  const message = error instanceof ApiError && error.status === 401
    ? "需要先登录才能查看归档。"
    : error.message;
  return <p className="border border-red-200 bg-red-50 p-4 text-sm text-red-800">{message}</p>;
}
