import { Link } from "@tanstack/react-router";
import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";

import type { AccountSummary } from "../lib/api/accounts";

const ArchivedAccounts = createContext<ReadonlySet<string>>(new Set());

export function ArchiveLinksProvider({ accounts, children }: {
  accounts: AccountSummary[] | undefined;
  children: ReactNode;
}) {
  const accountIds = useMemo(() => new Set(accounts?.map((account) => account.x_user_id)), [accounts]);
  return <ArchivedAccounts.Provider value={accountIds}>{children}</ArchivedAccounts.Provider>;
}

export function ArchiveAuthorLink({ author, children, className, label }: {
  author: { x_user_id: string; username: string | null };
  children: ReactNode;
  className?: string;
  label?: string;
}) {
  const accountIds = useContext(ArchivedAccounts);
  if (accountIds.has(author.x_user_id)) {
    return <Link to="/accounts/$xUserId" params={{ xUserId: author.x_user_id }} className={className} aria-label={label}>{children}</Link>;
  }
  const profileUrl = author.username ? `https://x.com/${encodeURIComponent(author.username)}`
    : /^\d+$/.test(author.x_user_id) ? `https://x.com/i/user/${author.x_user_id}` : null;
  if (profileUrl) return <a href={profileUrl} target="_blank" rel="noreferrer" className={className} aria-label={label}>{children}</a>;
  return <span className={className}>{children}</span>;
}
