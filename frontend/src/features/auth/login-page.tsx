import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { FormEvent, useState } from "react";

import { createSession } from "../../lib/api/auth";

export function LoginPage() {
  const [token, setToken] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const login = useMutation({
    mutationFn: createSession,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      await navigate({ to: "/" });
    }
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate(token);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md items-center px-5">
      <form onSubmit={submit} className="w-full border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-2xl font-semibold">ArchiveX</h1>
        <p className="mt-2 text-sm text-slate-600">输入管理员访问令牌以打开私人归档。</p>
        <label className="mt-6 block text-sm font-medium" htmlFor="token">访问令牌</label>
        <input
          id="token"
          type="password"
          autoComplete="current-password"
          required
          value={token}
          onChange={(event) => setToken(event.target.value)}
          className="mt-2 w-full border border-slate-300 px-3 py-2 outline-none focus:border-slate-950"
        />
        {login.error && <p className="mt-3 text-sm text-red-700">{login.error.message}</p>}
        <button
          type="submit"
          disabled={login.isPending}
          className="mt-5 w-full bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {login.isPending ? "正在验证..." : "进入归档"}
        </button>
      </form>
    </main>
  );
}
