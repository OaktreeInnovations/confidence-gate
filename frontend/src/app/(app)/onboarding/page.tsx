"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { apiPost } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ShieldCheck, Loader2 } from "lucide-react";

function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

export default function OnboardingPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function handleNameChange(value: string) {
    setName(value);
    if (!slugTouched) {
      setSlug(slugify(value));
    }
  }

  function handleSlugChange(value: string) {
    setSlugTouched(true);
    setSlug(slugify(value));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !slug) return;
    setLoading(true);
    setError(null);
    try {
      await apiPost("/api/orgs", { name: name.trim(), slug });
      router.replace("/");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to create organization";
      setError(msg);
      setLoading(false);
    }
  }

  const slugValid = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/.test(slug);

  return (
    <div className="flex min-h-[80vh] items-center justify-center">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <ShieldCheck className="h-6 w-6 text-primary" />
          </div>
          <CardTitle className="text-2xl">Create your organization</CardTitle>
          <CardDescription>
            Set up your workspace to start running tests.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="name">Organization name</Label>
              <Input
                id="name"
                placeholder="Acme Corp"
                value={name}
                onChange={(e) => handleNameChange(e.target.value)}
                disabled={loading}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="slug">
                URL slug
                <span className="ml-2 text-xs text-muted-foreground">
                  (letters, numbers, hyphens)
                </span>
              </Label>
              <Input
                id="slug"
                placeholder="acme-corp"
                value={slug}
                onChange={(e) => handleSlugChange(e.target.value)}
                disabled={loading}
                required
              />
              {slug && !slugValid && (
                <p className="text-xs text-destructive">
                  Must be 3–40 characters, start and end with a letter or number.
                </p>
              )}
            </div>
            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}
            <Button
              type="submit"
              className="w-full"
              disabled={loading || !name.trim() || !slugValid}
            >
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating…
                </>
              ) : (
                "Create organization"
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
