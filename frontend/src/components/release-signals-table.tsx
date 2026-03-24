"use client";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { signalSeverityVariant } from "@/lib/release-utils";
import type { ReleaseSignal } from "@/types/release-validation";

interface ReleaseSignalsTableProps {
  signals: ReleaseSignal[];
}

export function ReleaseSignalsTable({ signals }: ReleaseSignalsTableProps) {
  if (!signals.length) return null;

  return (
    <Table>
      <TableHeader>
        <TableRow className="bg-muted/30">
          <TableHead>Signal</TableHead>
          <TableHead className="w-[100px]">Category</TableHead>
          <TableHead className="w-[100px]">Severity</TableHead>
          <TableHead className="w-[80px] text-right">Score</TableHead>
          <TableHead>Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {signals.map((signal) => (
          <TableRow key={signal.name}>
            <TableCell className="font-medium">{signal.name}</TableCell>
            <TableCell>
              <span className="text-xs text-muted-foreground capitalize">
                {signal.category}
              </span>
            </TableCell>
            <TableCell>
              <Badge variant={signalSeverityVariant(signal.severity)}>
                {signal.severity}
              </Badge>
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {signal.score_contribution.toFixed(1)}
            </TableCell>
            <TableCell className="text-sm text-muted-foreground max-w-xs truncate">
              {signal.detail}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
