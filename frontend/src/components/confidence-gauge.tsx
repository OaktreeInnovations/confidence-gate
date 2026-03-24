"use client";

interface ConfidenceGaugeProps {
  score: number | null;
  grade: string | null;
  size?: number;
}

export function ConfidenceGauge({ score, grade, size = 120 }: ConfidenceGaugeProps) {
  const radius = (size - 12) / 2;
  const circumference = 2 * Math.PI * radius;
  const progress = score != null ? score / 100 : 0;
  const dashOffset = circumference * (1 - progress);

  const strokeColor =
    score == null
      ? "stroke-muted"
      : score >= 80
        ? "stroke-success"
        : score >= 50
          ? "stroke-warning"
          : "stroke-destructive";

  const textColor =
    score == null
      ? "text-muted-foreground"
      : score >= 80
        ? "text-success"
        : score >= 50
          ? "text-warning"
          : "text-destructive";

  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        {/* Background circle */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={6}
          className="text-muted/50"
        />
        {/* Progress circle */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth={6}
          strokeLinecap="round"
          className={`${strokeColor} transition-all duration-1000 ease-out`}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        {score != null ? (
          <>
            <span className={`text-2xl font-bold tabular-nums ${textColor}`}>
              {score}
            </span>
            {grade && (
              <span className={`text-xs font-semibold ${textColor}`}>
                {grade}
              </span>
            )}
          </>
        ) : (
          <span className="text-sm text-muted-foreground">--</span>
        )}
      </div>
    </div>
  );
}
