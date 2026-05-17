"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/contexts/auth-context";
import { apiGet } from "@/lib/api-client";
import { Loader2 } from "lucide-react";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [orgChecked, setOrgChecked] = useState(false);

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
      return;
    }

    if (!loading && user) {
      apiGet<{ org_id: string | null }>("/api/auth/me")
        .then(({ data }) => {
          if (!data.org_id && pathname !== "/onboarding") {
            router.replace("/onboarding");
          } else {
            setOrgChecked(true);
          }
        })
        .catch(() => setOrgChecked(true));
    }
  }, [user, loading, router, pathname]);

  if (loading || (user && !orgChecked && pathname !== "/onboarding")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return <>{children}</>;
}
