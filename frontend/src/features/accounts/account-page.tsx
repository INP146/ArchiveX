import { useParams } from "@tanstack/react-router";

export function AccountPage() {
  const { accountId } = useParams({ from: "/accounts/$accountId" });
  return (
    <section>
      <h1 className="text-2xl font-semibold">账号归档</h1>
      <p className="mt-2 text-sm text-slate-600">账号 #{accountId} 的时间线、筛选与帖子详情将在这里加载。</p>
    </section>
  );
}
