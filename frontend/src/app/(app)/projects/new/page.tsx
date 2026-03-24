"use client";

import { useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { apiPost } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { ArrowLeft } from "lucide-react";
import type { Project } from "@/types/project";

export default function NewProjectPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [globalSetup, setGlobalSetup] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const { data, status } = await apiPost<Project>("/api/projects", {
      name,
      description,
      base_url: baseUrl,
      global_setup: globalSetup,
    });

    setSubmitting(false);

    if (status === 201) {
      router.push(`/projects/${data.id}`);
    } else {
      setError(
        (data as unknown as { detail?: string })?.detail ??
          "Failed to create project",
      );
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Projects
      </Link>

      <Card>
        <CardHeader>
          <CardTitle>New Project</CardTitle>
          <CardDescription>Create a new project to organize your test cases</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g., User Authentication"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe the scope and purpose of this project..."
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="base_url">
                Base URL{" "}
                <span className="font-normal text-muted-foreground">(for API tests)</span>
              </Label>
              <Input
                id="base_url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="e.g., https://api.example.com"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="global_setup">
                Global Setup{" "}
                <span className="font-normal text-muted-foreground">(applied to all test cases)</span>
              </Label>
              <Textarea
                id="global_setup"
                rows={3}
                value={globalSetup}
                onChange={(e) => setGlobalSetup(e.target.value)}
                placeholder={"e.g., Login at https://app.example.com/sign-in\\nEmail: admin@example.com\\nPassword: secret123"}
              />
            </div>

            {error && (
              <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="flex gap-3">
              <Button type="submit" disabled={submitting}>
                {submitting ? "Creating..." : "Create Project"}
              </Button>
              <Button type="button" variant="outline" onClick={() => router.back()}>
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
