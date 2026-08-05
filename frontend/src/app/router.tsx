import { QueryClient } from "@tanstack/react-query";
import {
  Link,
  Outlet,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  useRouterState
} from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";

import { AccountPage } from "../features/accounts/account-page";
import { LoginPage } from "../features/auth/login-page";
import { DashboardPage } from "../features/dashboard/dashboard-page";
import { SyncRunsPage } from "../features/sync-runs/sync-runs-page";
import { deleteSession, getSession } from "../lib/api/auth";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30_000 }
  }
});

interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: AppShell
});

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: DashboardPage
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage
});

const accountRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/accounts/$accountId",
  component: AccountPage
});

const syncRunsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sync-runs",
  component: SyncRunsPage
});

const routeTree = rootRoute.addChildren([dashboardRoute, loginRoute, accountRoute, syncRunsRoute]);

export const router = createRouter({
  routeTree,
  context: { queryClient }
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

function AppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const session = useQuery({ queryKey: ["session"], queryFn: getSession });
  const logout = useMutation({
    mutationFn: deleteSession,
    onSuccess: async () => {
      await queryClient.invalidateQueries();
      await router.navigate({ to: "/login" });
    }
  });

  if (pathname === "/login" || pathname.startsWith("/accounts/")) {
    return <Outlet />;
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-950">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4 sm:px-6">
          <Link to="/" className="text-lg font-semibold">ArchiveX</Link>
          <nav className="flex items-center gap-5 text-sm text-slate-600">
            <Link to="/" activeProps={{ className: "font-semibold text-slate-950" }}>总览</Link>
            <Link to="/sync-runs" activeProps={{ className: "font-semibold text-slate-950" }}>同步记录</Link>
            {session.data?.authenticated ? (
              <button type="button" onClick={() => logout.mutate()} className="text-sm">退出</button>
            ) : <Link to="/login">登录</Link>}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6"><Outlet /></main>
    </div>
  );
}
