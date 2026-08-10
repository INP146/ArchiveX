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
import { AccountsPage } from "../features/accounts/accounts-page";
import { LoginPage } from "../features/auth/login-page";
import { DashboardPage } from "../features/dashboard/dashboard-page";
import { SettingsPage } from "../features/settings/settings-page";
import { TaskDetailsPage, TaskListPage, TaskSchedulesPage } from "../features/tasks/task-center-page";
import { ArchiveSidebar } from "../components/archive-sidebar";
import { getAccount, getAccounts } from "../lib/api/accounts";
import { deleteSession, getSession } from "../lib/api/auth";
import "./archive-shell.css";

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
  path: "/accounts/$xUserId",
  component: AccountPage
});

const accountsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/accounts",
  component: AccountsPage
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage
});

const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  component: TaskListPage
});

const taskDetailsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks/$taskId",
  component: TaskDetailRouteComponent
});

const taskSchedulesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks/schedules",
  component: TaskSchedulesPage
});

const routeTree = rootRoute.addChildren([
  dashboardRoute,
  loginRoute,
  accountsRoute,
  accountRoute,
  settingsRoute,
  tasksRoute,
  taskDetailsRoute,
  taskSchedulesRoute
]);

export const router = createRouter({
  routeTree,
  context: { queryClient }
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

function TaskDetailRouteComponent() {
  const { taskId } = taskDetailsRoute.useParams();
  return <TaskDetailsPage taskId={taskId} />;
}

function AppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const session = useQuery({ queryKey: ["session"], queryFn: getSession });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: getAccounts,
    enabled: session.data?.authenticated === true
  });
  const primaryAccountId = accounts.data?.[0]?.x_user_id;
  const primaryAccount = useQuery({
    queryKey: ["account", primaryAccountId],
    queryFn: () => getAccount(String(primaryAccountId)),
    enabled: primaryAccountId !== undefined
  });
  const logout = useMutation({
    mutationFn: deleteSession,
    onSuccess: async () => {
      await queryClient.invalidateQueries();
      await router.navigate({ to: "/login" });
    }
  });

  if (pathname === "/login") {
    return <Outlet />;
  }

  const operationsView = pathname.startsWith("/tasks");

  return (
    <div className={`x-app-shell ${operationsView ? "is-operations" : ""}`}>
      <ArchiveSidebar
        account={primaryAccount.data}
        viewer={session.data?.user}
        onLogout={() => logout.mutate()}
        onManageAccounts={() => void router.navigate({ to: "/accounts" })}
      />
      <main className="x-app-content"><Outlet /></main>
      <aside className="x-empty-rail x-empty-rail-right" aria-hidden="true" />
    </div>
  );
}
