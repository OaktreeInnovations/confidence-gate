"use client";

import Link from "next/link";
import { useAuth } from "@/contexts/auth-context";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { FolderKanban, TestTubeDiagonal, Play, ArrowRight } from "lucide-react";

export default function DashboardPage() {
  const { user } = useAuth();

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="mt-1 text-muted-foreground">
          Welcome back, <span className="text-foreground">{user?.email}</span>
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="group relative overflow-hidden">
          <CardHeader className="pb-2">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <FolderKanban className="h-5 w-5 text-primary" />
            </div>
            <CardTitle className="mt-3 text-base">Projects</CardTitle>
            <CardDescription>
              Organize your test cases into projects
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/projects">
              <Button variant="ghost" size="sm" className="gap-1 px-0 text-primary">
                View projects <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </Link>
          </CardContent>
        </Card>

        <Card className="group relative overflow-hidden">
          <CardHeader className="pb-2">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <TestTubeDiagonal className="h-5 w-5 text-primary" />
            </div>
            <CardTitle className="mt-3 text-base">Test Cases</CardTitle>
            <CardDescription>
              Define and manage your test scenarios
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/test-cases">
              <Button variant="ghost" size="sm" className="gap-1 px-0 text-primary">
                View test cases <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </Link>
          </CardContent>
        </Card>

        <Card className="group relative overflow-hidden">
          <CardHeader className="pb-2">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <Play className="h-5 w-5 text-primary" />
            </div>
            <CardTitle className="mt-3 text-base">Test Runs</CardTitle>
            <CardDescription>
              Monitor AI-powered test execution results
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/test-runs">
              <Button variant="ghost" size="sm" className="gap-1 px-0 text-primary">
                View test runs <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
