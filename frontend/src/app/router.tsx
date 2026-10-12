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
import { type UIEvent, useEffect, useRef, useState } from "react";

import { AccountPage } from "../features/accounts/account-page";
import { AccountsPage } from "../features/accounts/accounts-page";
import { LoginPage } from "../features/auth/login-page";
import { DashboardPage } from "../features/dashboard/dashboard-page";
import { PostPage } from "../features/timeline/post-page";
import { MediaPage } from "../features/timeline/media-page";
import { SearchPage } from "../features/search/search-page";
import { SettingsPage } from "../features/settings/settings-page";
import { TaskDetailsPage, TaskListPage, TaskSchedulesPage } from "../features/tasks/task-center-page";
import { ArchiveSidebar } from "../components/archive-sidebar";
import { ArchiveLinksProvider } from "../components/archive-author-link";
import { getAccounts } from "../lib/api/accounts";
import { parseArchiveSearch, stringifyArchiveSearch, validatePostSearch, validateTaskSearch } from "../lib/navigation-search";
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

const postRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/posts/$tweetId",
  component: PostPage
});

const searchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/search",
  validateSearch: validatePostSearch,
  component: SearchPage
});

const mediaRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/media",
  component: MediaPage
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage
});

const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  validateSearch: validateTaskSearch,
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
  searchRoute,
  accountsRoute,
  accountRoute,
  postRoute,
  mediaRoute,
  settingsRoute,
  tasksRoute,
  taskDetailsRoute,
  taskSchedulesRoute
]);

export const router = createRouter({
  routeTree,
  parseSearch: parseArchiveSearch,
  stringifySearch: stringifyArchiveSearch,
  scrollRestoration: true,
  scrollToTopSelectors: [".x-app-content", ".x-profile-column"],
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
  const [mobileHeaderHidden, setMobileHeaderHidden] = useState(false);
  const lastMobileScrollTop = useRef(0);
  const mobileScrollTravel = useRef(0);
  const session = useQuery({ queryKey: ["session"], queryFn: getSession });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: getAccounts,
    enabled: session.data?.authenticated === true
  });
  const logout = useMutation({
    mutationFn: deleteSession,
    onSuccess: async () => {
      await queryClient.invalidateQueries();
      await router.navigate({ to: "/login" });
    }
  });

  useEffect(() => {
    lastMobileScrollTop.current = 0;
    mobileScrollTravel.current = 0;
    setMobileHeaderHidden(false);
  }, [pathname]);

  function handleAppScroll(event: UIEvent<HTMLDivElement>) {
    if (!window.matchMedia("(max-width: 700px)").matches) return;
    const scrollContainer = event.target;
    if (!(scrollContainer instanceof HTMLElement)) return;
    if (!scrollContainer.matches(".x-app-content, .x-profile-column")) return;

    const scrollTop = Math.max(0, scrollContainer.scrollTop);
    const delta = scrollTop - lastMobileScrollTop.current;
    lastMobileScrollTop.current = scrollTop;

    if (scrollTop <= 8) {
      mobileScrollTravel.current = 0;
      setMobileHeaderHidden(false);
      return;
    }
    if (delta === 0) return;

    if (
      (delta > 0 && mobileScrollTravel.current < 0)
      || (delta < 0 && mobileScrollTravel.current > 0)
    ) {
      mobileScrollTravel.current = 0;
    }
    mobileScrollTravel.current += delta;

    if (mobileScrollTravel.current >= 18) {
      setMobileHeaderHidden(true);
      mobileScrollTravel.current = 0;
    } else if (mobileScrollTravel.current <= -10) {
      setMobileHeaderHidden(false);
      mobileScrollTravel.current = 0;
    }
  }

  if (pathname === "/login") {
    return <Outlet />;
  }

  const operationsView = pathname.startsWith("/tasks");

  return (
    <ArchiveLinksProvider accounts={accounts.data}>
      <div
        className={`x-app-shell ${operationsView ? "is-operations" : ""} ${mobileHeaderHidden ? "is-mobile-header-hidden" : ""}`}
        onScrollCapture={handleAppScroll}
      >
        <ArchiveSidebar
          accountCount={accounts.data?.length ?? 0}
          viewer={session.data?.user}
          onLogout={() => logout.mutate()}
          onManageAccounts={() => void router.navigate({ to: "/accounts" })}
        />
        <main className="x-app-content"><Outlet /></main>
        <aside className="x-empty-rail x-empty-rail-right" aria-hidden="true" />
      </div>
    </ArchiveLinksProvider>
  );
}
