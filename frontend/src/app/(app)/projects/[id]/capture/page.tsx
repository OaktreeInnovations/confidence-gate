"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Circle, Copy, Check, Loader2, Save, Square, ExternalLink } from "lucide-react";
import { apiPost, apiGet } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useToast } from "@/components/ui/toast";
import { CapturedStepsList } from "@/components/capture/captured-steps-list";
import type {
  CaptureSession,
  CapturedStep,
  CreateSessionResponse,
  StopSessionResponse,
  SaveSessionResponse,
} from "@/types/capture";

type Phase = "setup" | "recording" | "preview";

export default function CapturePage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const projectId = params.id as string;

  const [phase, setPhase] = useState<Phase>("setup");
  const [title, setTitle] = useState("");
  const [targetUrl, setTargetUrl] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const [steps, setSteps] = useState<CapturedStep[]>([]);
  const [isStarting, setIsStarting] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveTitle, setSaveTitle] = useState("");
  const [inlineScript, setInlineScript] = useState("");
  const [copied, setCopied] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const targetTabRef = useRef<Window | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const eventCountRef = useRef(0);

  // ── postMessage relay ──────────────────────────────────────────────────────
  // The capture script sends events via window.opener.postMessage to avoid
  // mixed-content blocks (HTTPS target site → HTTP localhost backend).
  // We receive them here and forward to the backend via same-origin API call.
  const handleMessage = useCallback(async (event: MessageEvent) => {
    if (
      event.data?.type !== "__cg_events" ||
      event.data?.session_id !== sessionIdRef.current
    ) return;

    const events: unknown[] = event.data.events ?? [];
    if (!events.length) return;

    eventCountRef.current += events.length;
    setEventCount(eventCountRef.current);

    await apiPost(`/api/capture/sessions/${sessionIdRef.current}/events`, { events });
  }, []);

  useEffect(() => {
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [handleMessage]);

  // ── Polling fallback (for direct-POST path) ────────────────────────────────
  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const startPolling = useCallback((sid: string) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      const { data, status } = await apiGet<CaptureSession>(`/api/capture/sessions/${sid}`);
      if (status === 200 && data.event_count > eventCountRef.current) {
        eventCountRef.current = data.event_count;
        setEventCount(data.event_count);
      }
    }, 2000);
  }, [stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  async function handleStart() {
    if (!title.trim()) { toast("Enter a test title", "error"); return; }
    if (!targetUrl.trim()) { toast("Enter your app URL", "error"); return; }

    setIsStarting(true);

    const { data, status } = await apiPost<CreateSessionResponse>("/api/capture/sessions", {
      project_id: projectId,
      base_url: targetUrl,
      title,
    });

    if (status !== 201) {
      setIsStarting(false);
      toast("Failed to start capture session", "error");
      return;
    }

    const sid = data.session_id;
    sessionIdRef.current = sid;
    eventCountRef.current = 0;

    // Fetch the full inline script (same-origin, no CORS)
    const scriptRes = await fetch(`/api/capture/script?session_id=${sid}`);
    const scriptText = await scriptRes.text();

    setIsStarting(false);
    setSaveTitle(title);
    setSessionId(sid);
    setInlineScript(scriptText);
    setEventCount(0);

    // Open target URL in new tab — window.opener will be set in that tab
    targetTabRef.current = window.open(targetUrl, "_blank");

    setPhase("recording");
    startPolling(sid);
  }

  async function handleCopy() {
    await navigator.clipboard.writeText(inlineScript);
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  }

  async function handleStop() {
    if (!sessionId) return;
    stopPolling();
    setIsStopping(true);

    // Wait briefly so any in-flight postMessage events can be forwarded
    await new Promise((r) => setTimeout(r, 1200));

    const { data, status } = await apiPost<StopSessionResponse>(
      `/api/capture/sessions/${sessionId}/stop`
    );
    setIsStopping(false);
    if (status !== 200) { toast("Failed to stop session", "error"); return; }
    setSteps(data.steps);
    setPhase("preview");
  }

  async function handleSave() {
    if (!sessionId) return;
    setIsSaving(true);
    const { data, status } = await apiPost<SaveSessionResponse>(
      `/api/capture/sessions/${sessionId}/save`,
      { title: saveTitle, base_url: targetUrl }
    );
    setIsSaving(false);
    if (status !== 201) { toast("Failed to save test case", "error"); return; }
    toast("Test case saved", "success");
    router.push(`/test-cases/${data.test_case_id}`);
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center gap-3">
        <Link href={`/projects/${projectId}`}>
          <Button variant="ghost" size="icon"><ArrowLeft className="h-4 w-4" /></Button>
        </Link>
        <div>
          <h1 className="text-xl font-semibold">Quick Capture</h1>
          <p className="text-sm text-muted-foreground">Record browser interactions and save them as a test case</p>
        </div>
      </div>

      {/* ── Setup ── */}
      {phase === "setup" && (
        <Card className="max-w-lg">
          <CardHeader><CardTitle className="text-base">New Capture Session</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="cap-title">Test title</Label>
              <Input id="cap-title" placeholder="e.g. User login flow" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap-url">App URL</Label>
              <Input id="cap-url" placeholder="https://your-app.example.com" value={targetUrl} onChange={(e) => setTargetUrl(e.target.value)} />
            </div>
            <Button onClick={handleStart} disabled={isStarting} className="w-full">
              {isStarting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Circle className="h-4 w-4 fill-red-500 text-red-500" />}
              Start Recording
            </Button>
          </CardContent>
        </Card>
      )}

      {/* ── Recording ── */}
      {phase === "recording" && sessionId && (
        <div className="max-w-lg space-y-4">
          <Card className="border-destructive/40 bg-destructive/5">
            <CardContent className="pt-5 space-y-4">
              <div className="flex items-center gap-2">
                <Circle className="h-3 w-3 fill-destructive text-destructive animate-pulse" />
                <span className="font-medium text-sm">Recording</span>
                <span className="ml-auto tabular-nums text-sm text-muted-foreground">{eventCount} events</span>
              </div>

              <ol className="space-y-3 text-sm">
                <li className="flex gap-3 items-start">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">1</span>
                  <div className="space-y-1.5 flex-1">
                    <span>Your app opened in a new tab. Switch to it now.</span>
                    <Button size="sm" variant="outline" className="w-full" onClick={() => targetTabRef.current?.focus()}>
                      <ExternalLink className="h-3.5 w-3.5" />
                      Focus app tab
                    </Button>
                  </div>
                </li>
                <li className="flex gap-3 items-start">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">2</span>
                  <div className="space-y-1.5 flex-1">
                    <span>In the app tab: <kbd className="rounded border bg-muted px-1.5 py-0.5 font-mono text-xs">F12</kbd> → <strong>Console</strong> → paste this snippet → Enter</span>
                    <Button size="sm" variant="outline" className="w-full" onClick={handleCopy}>
                      {copied
                        ? <><Check className="h-3.5 w-3.5 text-green-500" /> Copied!</>
                        : <><Copy className="h-3.5 w-3.5" /> Copy snippet</>}
                    </Button>
                  </div>
                </li>
                <li className="flex gap-3 items-start">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">3</span>
                  <span>Interact with your app. The event counter above updates as you go.</span>
                </li>
              </ol>

              <Button variant="destructive" className="w-full" onClick={handleStop} disabled={isStopping}>
                {isStopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                Stop Recording
              </Button>
            </CardContent>
          </Card>
        </div>
      )}

      {/* ── Preview ── */}
      {phase === "preview" && (
        <div className="max-w-2xl space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">{steps.length} step{steps.length !== 1 ? "s" : ""} captured</h2>
            <div className="flex items-center gap-2">
              <Input className="h-8 w-56 text-sm" placeholder="Test case title" value={saveTitle} onChange={(e) => setSaveTitle(e.target.value)} />
              <Button size="sm" onClick={handleSave} disabled={isSaving || !saveTitle.trim()}>
                {isSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                Save Test Case
              </Button>
            </div>
          </div>
          <CapturedStepsList steps={steps} />
        </div>
      )}
    </div>
  );
}
