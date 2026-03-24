"use client";

import { AuthGuard } from "@/components/auth-guard";
import { ErrorBoundary } from "@/components/error-boundary";
import { Sidebar } from "@/components/sidebar";
import { ToastProvider } from "@/components/ui/toast";
import { TooltipProvider } from "@/components/ui/tooltip";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      <TooltipProvider>
        <ToastProvider>
          <div className="flex h-screen print:block print:h-auto">
            <div className="print:hidden">
              <Sidebar />
            </div>
            <main className="flex-1 overflow-y-auto p-6 print:overflow-visible print:w-full">
              <ErrorBoundary>{children}</ErrorBoundary>
            </main>
          </div>
        </ToastProvider>
      </TooltipProvider>
    </AuthGuard>
  );
}
