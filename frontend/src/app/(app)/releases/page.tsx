"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { apiGet } from "@/lib/api-client";
import {
  validationStatusBadgeVariant,
  recommendationVariant,
  recommendationLabel,
  scoreColor,
  gradeColor,
} from "@/lib/release-utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import type {
  ReleaseValidation,
  ReleaseValidationListResponse,
  ReleaseValidationStatus,
} from "@/types/release-validation";

export default function ReleasesPage() {
  const router = useRouter();
  const [data, setData] = useState<ReleaseValidationListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchValidations = useCallback(async () => {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", "20");

    const { data: result, status } = await apiGet<ReleaseValidationListResponse>(
      `/api/release-validations?${params.toString()}`,
    );
    if (status === 200) {
      setData(result);
    }
    setLoading(false);
  }, [page]);

  useEffect(() => {
    setLoading(true);
    fetchValidations();
  }, [fetchValidations]);

  // Poll while any validation is active
  useEffect(() => {
    const hasActive = data?.items.some(
      (v) => v.status === "running" || v.status === "computing" || v.status === "pending",
    );

    if (hasActive) {
      intervalRef.current = setInterval(fetchValidations, 3000);
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [data, fetchValidations]);

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Release Validations</h1>
      </div>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
          <p className="text-sm text-muted-foreground">Loading validations...</p>
        </div>
      ) : data?.items.length === 0 ? (
        <div className="py-12 text-center">
          <p className="text-muted-foreground">
            No release validations yet. Run one from a project page.
          </p>
        </div>
      ) : (
        <Card className="overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/30">
                <TableHead>Project</TableHead>
                <TableHead>Title</TableHead>
                <TableHead className="w-[80px] text-center">Score</TableHead>
                <TableHead className="w-[60px] text-center">Grade</TableHead>
                <TableHead className="w-[160px]">Recommendation</TableHead>
                <TableHead className="w-[120px]">Status</TableHead>
                <TableHead className="w-[120px]">Progress</TableHead>
                <TableHead className="w-[140px]">Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.items.map((v) => {
                const isActive =
                  v.status === "running" || v.status === "computing" || v.status === "pending";
                return (
                  <TableRow
                    key={v.id}
                    onClick={() => router.push(`/releases/${v.id}`)}
                    className="cursor-pointer"
                  >
                    <TableCell className="font-medium">
                      {v.project_name || "Unknown Project"}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {v.title || ""}
                    </TableCell>
                    <TableCell className="text-center">
                      {v.confidence_score != null ? (
                        <span className={`font-bold tabular-nums ${scoreColor(v.confidence_score)}`}>
                          {v.confidence_score}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">--</span>
                      )}
                    </TableCell>
                    <TableCell className="text-center">
                      {v.confidence_grade ? (
                        <span className={`font-bold ${gradeColor(v.confidence_grade)}`}>
                          {v.confidence_grade}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">--</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {v.recommendation ? (
                        <Badge variant={recommendationVariant(v.recommendation)}>
                          {recommendationLabel(v.recommendation)}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">--</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={validationStatusBadgeVariant[v.status as ReleaseValidationStatus] ?? "muted"}
                        className="gap-1.5"
                      >
                        {isActive && <Loader2 className="h-3 w-3 animate-spin" />}
                        {v.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm tabular-nums text-muted-foreground">
                        {v.completed_runs}/{v.total_runs}
                      </span>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {new Date(v.created_at).toLocaleString(undefined, {
                        month: "2-digit",
                        day: "2-digit",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}

      {data && data.total > 0 && totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            {data.total} validation{data.total !== 1 ? "s" : ""}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <span className="text-sm text-muted-foreground">
              {page} / {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
