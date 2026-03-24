"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiDelete } from "@/lib/api-client";
import { runStatusBadgeVariant, formatDuration } from "@/lib/test-run-utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/components/ui/toast";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Search,
  Filter,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronRight as ChevronExpand,
  Loader2,
  Trash2,
  MoreVertical,
} from "lucide-react";
import type { BatchListResponse, TestRunBatch, TestRunStatus } from "@/types/test-run";

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "passed", label: "Passed" },
  { value: "failed", label: "Failed" },
  { value: "error", label: "Error" },
];

export default function TestRunsPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [data, setData] = useState<BatchListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [showFilterMenu, setShowFilterMenu] = useState(false);
  const [expandedBatches, setExpandedBatches] = useState<Set<string>>(new Set());
  const [deletingBatch, setDeletingBatch] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const filterRef = useRef<HTMLDivElement>(null);

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchQuery);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Close filter menu on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) {
        setShowFilterMenu(false);
      }
    }
    if (showFilterMenu) {
      document.addEventListener("mousedown", handleClick);
    }
    return () => document.removeEventListener("mousedown", handleClick);
  }, [showFilterMenu]);

  const fetchBatches = useCallback(async () => {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", "20");
    if (statusFilter) params.set("status", statusFilter);
    if (debouncedSearch) params.set("search", debouncedSearch);

    const { data: result, status } = await apiGet<BatchListResponse>(
      `/api/test-runs/batches?${params.toString()}`,
    );
    if (status === 200) {
      setData(result);
    }
    setLoading(false);
  }, [page, statusFilter, debouncedSearch]);

  useEffect(() => {
    setLoading(true);
    fetchBatches();
  }, [fetchBatches]);

  // Auto-poll when there are active batches
  useEffect(() => {
    const hasActive = data?.items.some(
      (b) => b.status === "queued" || b.status === "running",
    );

    if (hasActive) {
      intervalRef.current = setInterval(fetchBatches, 3000);
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [data, fetchBatches]);

  useEffect(() => {
    setPage(1);
  }, [statusFilter]);

  function toggleBatch(batchId: string) {
    setExpandedBatches((prev) => {
      const next = new Set(prev);
      if (next.has(batchId)) next.delete(batchId);
      else next.add(batchId);
      return next;
    });
  }

  function handleDeleteBatch(batchId: string) {
    setConfirmAction({
      title: "Delete batch",
      description: "Delete this test run batch? This cannot be undone.",
      confirmLabel: "Delete",
      onConfirm: async () => {
        setConfirmAction(null);
        setDeletingBatch(batchId);
        const { status } = await apiDelete(`/api/test-runs/batch/${batchId}`);
        setDeletingBatch(null);
        if (status === 200 || status === 204) {
          toast("Batch deleted", "success");
        } else {
          toast("Failed to delete batch", "error");
        }
        fetchBatches();
      },
    });
  }

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">All Test Runs</h1>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>

        {/* Filter dropdown */}
        <div className="relative" ref={filterRef}>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowFilterMenu(!showFilterMenu)}
            className={statusFilter ? "border-primary text-primary" : ""}
          >
            <Filter className="h-4 w-4" />
            Filter
            {statusFilter && (
              <Badge variant="default" className="ml-1.5 px-1.5 py-0 text-[10px]">
                1
              </Badge>
            )}
          </Button>
          {showFilterMenu && (
            <div className="absolute left-0 z-50 mt-1 w-48 rounded-md border bg-popover p-2 shadow-md">
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">Status</p>
              {STATUS_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => {
                    setStatusFilter(opt.value);
                    setShowFilterMenu(false);
                  }}
                  className={`flex w-full items-center rounded-sm px-2 py-1.5 text-sm transition-colors ${
                    statusFilter === opt.value
                      ? "bg-accent text-accent-foreground"
                      : "hover:bg-accent/50"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Sort indicator */}
        <Button variant="outline" size="sm" className="cursor-default">
          <ArrowUpDown className="h-4 w-4" />
          Sort: Created At
        </Button>
      </div>

      {/* Main table */}
      {loading ? (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
          <p className="text-sm text-muted-foreground">Loading test runs...</p>
        </div>
      ) : data?.items.length === 0 ? (
        <div className="py-12 text-center">
          <p className="text-muted-foreground">No test runs found.</p>
        </div>
      ) : (
        <Card className="overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/30">
                <TableHead className="w-[40px]" />
                <TableHead>Creation Name</TableHead>
                <TableHead className="w-[200px]">Status</TableHead>
                <TableHead className="w-[50px]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.items.map((batch) => {
                const isExpanded = expandedBatches.has(batch.batch_id);
                const isActive =
                  batch.status === "queued" || batch.status === "running";

                return (
                  <BatchRow
                    key={batch.batch_id}
                    batch={batch}
                    isExpanded={isExpanded}
                    isActive={isActive}
                    deleting={deletingBatch === batch.batch_id}
                    onToggle={() => toggleBatch(batch.batch_id)}
                    onDelete={() => handleDeleteBatch(batch.batch_id)}
                    onRunClick={(runId) => router.push(`/test-runs/${runId}`)}
                  />
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Pagination */}
      {data && data.total > 0 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            {data.total} batch{data.total !== 1 ? "es" : ""}
          </p>
          {totalPages > 1 && (
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
          )}
        </div>
      )}

      <ConfirmDialog
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        description={confirmAction?.description ?? ""}
        confirmLabel={confirmAction?.confirmLabel ?? "Confirm"}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  );
}

/* --- Batch Row Component --- */

function BatchRow({
  batch,
  isExpanded,
  isActive,
  deleting,
  onToggle,
  onDelete,
  onRunClick,
}: {
  batch: TestRunBatch;
  isExpanded: boolean;
  isActive: boolean;
  deleting: boolean;
  onToggle: () => void;
  onDelete: () => void;
  onRunClick: (runId: string) => void;
}) {
  return (
    <>
      {/* Batch header row */}
      <TableRow
        onClick={onToggle}
        className={`cursor-pointer transition-colors hover:bg-muted/50 ${isActive ? "bg-primary/5" : ""}`}
      >
        <TableCell className="px-3">
          <div className="flex h-5 w-5 items-center justify-center">
            {isExpanded ? (
              <ChevronDown className="h-4 w-4 text-muted-foreground" />
            ) : (
              <ChevronExpand className="h-4 w-4 text-muted-foreground" />
            )}
          </div>
        </TableCell>
        <TableCell>
          <span className="font-medium">
            {batch.batch_name || batch.project_name || `Batch ${batch.batch_id.slice(0, 8)}`}
          </span>
        </TableCell>
        <TableCell>
          <div className="space-y-0.5">
            <Badge
              variant={runStatusBadgeVariant[batch.status as TestRunStatus] ?? "muted"}
              className="gap-1.5"
            >
              {isActive && <Loader2 className="h-3 w-3 animate-spin" />}
              {batch.status === "passed"
                ? "Pass"
                : batch.status === "failed"
                  ? "Failed"
                  : batch.status === "error"
                    ? "Warning"
                    : batch.status.charAt(0).toUpperCase() + batch.status.slice(1)}
            </Badge>
            <p className="text-xs text-muted-foreground">
              {new Date(batch.created_at).toLocaleString(undefined, {
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </p>
          </div>
        </TableCell>
        <TableCell className="px-2" onClick={(e) => e.stopPropagation()}>
          <DropdownMenu>
            <DropdownMenuTrigger className="rounded-md p-1.5 text-muted-foreground/50 hover:bg-muted hover:text-foreground transition-colors">
              <MoreVertical className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem
                variant="destructive"
                onClick={onDelete}
                disabled={deleting}
              >
                {deleting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Trash2 className="h-3.5 w-3.5" />
                )}
                {deleting ? "Deleting..." : "Delete batch"}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </TableCell>
      </TableRow>

      {/* Active batch progress bar */}
      {isActive && isExpanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={4} className="p-0">
            <div className="h-0.5 w-full bg-muted overflow-hidden">
              <div
                className="h-full bg-primary transition-all duration-500 ease-out"
                style={{
                  width: `${batch.total_runs > 0 ? ((batch.passed + batch.failed + batch.error) / batch.total_runs) * 100 : 0}%`,
                }}
              />
            </div>
          </TableCell>
        </TableRow>
      )}

      {/* Expanded runs sub-table */}
      {isExpanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={4} className="p-0">
            <div className="border-t bg-muted/10">
              <Table>
                <TableHeader>
                  <TableRow className="bg-muted/20">
                    <TableHead className="pl-12">Test Name</TableHead>
                    <TableHead className="w-[80px]">Type</TableHead>
                    <TableHead className="w-[100px]">Latest Status</TableHead>
                    <TableHead className="w-[80px]">Steps</TableHead>
                    <TableHead className="w-[120px]">Created At</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {batch.runs.map((run) => {
                    const runActive =
                      run.status === "queued" || run.status === "running";
                    return (
                      <TableRow
                        key={run.id}
                        onClick={() => onRunClick(run.id)}
                        className="cursor-pointer"
                      >
                        <TableCell className="pl-12 font-medium text-sm">
                          {run.test_case_title}
                        </TableCell>
                        <TableCell>
                          <span className="text-xs text-muted-foreground">
                            {run.test_type === "api" ? "Backend" : "Frontend"}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              runStatusBadgeVariant[run.status as TestRunStatus]
                            }
                            className="gap-1.5"
                          >
                            {runActive && (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            )}
                            {run.status === "passed"
                              ? "Pass"
                              : run.status === "failed"
                                ? "Failed"
                                : run.status.charAt(0).toUpperCase() + run.status.slice(1)}
                          </Badge>
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground text-sm">
                          {run.passed_steps}/{run.total_steps}
                        </TableCell>
                        <TableCell className="text-muted-foreground text-sm">
                          {new Date(run.created_at).toLocaleDateString()}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
