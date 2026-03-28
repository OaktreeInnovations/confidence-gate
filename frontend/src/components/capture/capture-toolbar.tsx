"use client";

import { useState } from "react";
import { Circle, Square, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface CaptureToolbarProps {
  sessionId: string | null;
  isRecording: boolean;
  isStopping: boolean;
  eventCount: number;
  onStop: () => void;
}

export function CaptureToolbar({
  sessionId,
  isRecording,
  isStopping,
  eventCount,
  onStop,
}: CaptureToolbarProps) {
  if (!sessionId) return null;

  return (
    <div className="flex items-center gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-2">
      {isRecording ? (
        <span className="flex items-center gap-1.5 text-sm font-medium text-destructive">
          <Circle className="h-3 w-3 fill-destructive animate-pulse" />
          Recording
        </span>
      ) : (
        <span className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
          <Square className="h-3 w-3" />
          Stopped
        </span>
      )}
      <span className="text-sm text-muted-foreground">{eventCount} events captured</span>
      {isRecording && (
        <Button
          size="sm"
          variant="destructive"
          onClick={onStop}
          disabled={isStopping}
        >
          {isStopping ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Square className="h-3.5 w-3.5" />
          )}
          Stop
        </Button>
      )}
    </div>
  );
}
