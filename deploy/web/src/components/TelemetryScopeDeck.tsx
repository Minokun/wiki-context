import React, { useMemo, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { LogStatsResponse } from "../types";

interface TelemetryScopeDeckProps {
  stats: LogStatsResponse | null;
  sseConnected: boolean;
  vaultCount: number;
  latestFrame?: { tool: string; vaultId: string; client: string; elapsedMs: number } | null;
}

function buildSparkPath(values: number[], w = 136, h = 28): { line: string; area: string } {
  if (values.length === 0) return { line: "", area: "" };
  const max = Math.max(1, ...values);
  const pts = values.map((v, idx) => {
    const x = values.length === 1 ? w / 2 : (idx / (values.length - 1)) * w;
    const y = h - 3 - (v / max) * (h - 7);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const line = pts.join(" ");
  const area = `M0,${h} L${pts.join(" L")} L${w},${h} Z`;
  return { line, area };
}

export const TelemetryScopeDeck: React.FC<TelemetryScopeDeckProps> = ({
  stats,
  sseConnected,
  vaultCount,
  latestFrame,
}) => {
  const [collapsed, setCollapsed] = useState(true);
  const [showStdio, setShowStdio] = useState(true);
  const [showHttp, setShowHttp] = useState(true);
  const [showBars, setShowBars] = useState(true);
  const [activeIdx, setActiveIdx] = useState<number>(23);

  const bins = useMemo(() => {
    const raw = stats?.hourly || [];
    if (raw.length === 24) {
      return raw.map((b) => ({
        offset: b.hour_offset,
        total: b.count || 0,
        stdio: b.stdio ?? b.count ?? 0,
        http: b.http ?? 0,
        blocked: b.acl_blocked ?? 0,
        p50: b.p50_ms ?? 0,
      }));
    }
    return Array.from({ length: 24 }, (_, i) => ({
      offset: i - 23,
      total: 0,
      stdio: 0,
      http: 0,
      blocked: 0,
      p50: 0,
    }));
  }, [stats]);

  const totalCalls = stats?.total_calls ?? 0;
  const stdioCalls = stats?.stdio_calls ?? totalCalls;
  const httpCalls = stats?.http_calls ?? 0;
  const blockedCalls = stats?.acl_blocked_calls ?? 0;
  const p50 = stats?.p50_ms ?? 0;
  const p95 = stats?.p95_ms ?? 0;
  const zeroHits = stats?.zero_hit_count ?? 0;

  const maxY = useMemo(() => {
    const m = Math.max(4, ...bins.map((b) => Math.max(b.total, b.stdio, b.http)));
    const rawCeil = Math.ceil(m * 1.15);
    return rawCeil % 2 === 0 ? rawCeil : rawCeil + 1;
  }, [bins]);

  const totalSpark = useMemo(() => buildSparkPath(bins.map((b) => b.total)), [bins]);
  const blockedSpark = useMemo(() => buildSparkPath(bins.map((b) => b.blocked)), [bins]);

  // Calibrated SVG coordinate system with dedicated left Y-axis gutter and bottom X-axis ruler
  const W = 960;
  const H = 166;
  const LEFT = 46;
  const RIGHT = 942;
  const TOP = 16;
  const BOT = 136;
  const plotW = RIGHT - LEFT;
  const plotH = BOT - TOP;
  const stepX = plotW / 23;

  const xAt = (i: number) => LEFT + i * stepX;
  const yAt = (val: number) => BOT - (Math.min(val, maxY) / maxY) * plotH;

  const buildSmoothLineAndArea = (vals: number[]) => {
    if (vals.length === 0) return { line: "", area: "" };
    const pts = vals.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`);
    const line = pts.join(" ");
    const area = `M${xAt(0).toFixed(1)},${BOT} L${pts.join(" L")} L${xAt(vals.length - 1).toFixed(1)},${BOT} Z`;
    return { line, area };
  };

  const stdioGeo = useMemo(() => buildSmoothLineAndArea(bins.map((b) => b.stdio)), [bins, maxY]);
  const httpGeo = useMemo(() => buildSmoothLineAndArea(bins.map((b) => b.http)), [bins, maxY]);

  const safeIdx = Math.min(bins.length - 1, Math.max(0, activeIdx));
  const curBin = bins[safeIdx] || bins[bins.length - 1];
  const curHourLabel = curBin.offset === 0 ? "当前小时" : `${Math.abs(curBin.offset)} 小时前`;

  const majorTicksX = [
    { idx: 0, label: "24h前" },
    { idx: 6, label: "18h前" },
    { idx: 12, label: "12h前" },
    { idx: 18, label: "6h前" },
    { idx: 23, label: "当前" },
  ];

  return (
    <section className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs">
      {/* Unified Header Bar (1-line compact summary when collapsed) */}
      <div
        className={`flex flex-wrap items-center justify-between gap-2 bg-[var(--bg-subtle)] px-4 py-2.5 ${
          collapsed ? "" : "border-b border-[var(--border-subtle)]"
        }`}
      >
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 rounded-full ${
                sseConnected ? "bg-emerald-500" : "bg-amber-500"
              }`}
            />
            <span className="text-xs font-semibold text-[var(--ink-primary)]">
              近 24 小时使用概况
            </span>
          </div>

          {collapsed ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ink-secondary)]">
              <span>查询 {totalCalls} 次</span>
              <span className="text-[var(--ink-muted)]">·</span>
              <span>平均响应 {p50.toFixed(0)} ms</span>
              {zeroHits > 0 && (
                <>
                  <span className="text-[var(--ink-muted)]">·</span>
                  <span>未查到 {zeroHits} 次</span>
                </>
              )}
              {blockedCalls > 0 && (
                <>
                  <span className="text-[var(--ink-muted)]">·</span>
                  <span>权限隐藏 {blockedCalls} 次</span>
                </>
              )}
            </div>
          ) : (
            latestFrame && (
              <span className="hidden items-center gap-1.5 rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-0.5 text-xs text-[var(--ink-secondary)] xl:inline-flex">
                <span>最近查询</span>
                <span className="font-semibold text-[var(--accent)]">{latestFrame.client}</span>
                <span className="text-[var(--ink-muted)]">/</span>
                <span>{latestFrame.vaultId}</span>
                <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                  {latestFrame.elapsedMs.toFixed(1)} ms
                </span>
              </span>
            )
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!collapsed && (
            <div className="flex items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] p-0.5 text-xs">
              <button
                type="button"
                onClick={() => setShowStdio((v) => !v)}
                className={`inline-flex items-center gap-1.5 rounded-[5px] px-2.5 py-1 font-medium ${
                  showStdio
                    ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)]"
                    : "bg-transparent text-[var(--ink-muted)] opacity-55"
                }`}
              >
                <span className="h-2 w-2 rounded-full bg-[var(--chart-stdio)]" />
                本机查询 ({stdioCalls})
              </button>
              <button
                type="button"
                onClick={() => setShowHttp((v) => !v)}
                className={`inline-flex items-center gap-1.5 rounded-[5px] px-2.5 py-1 font-medium ${
                  showHttp
                    ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)]"
                    : "bg-transparent text-[var(--ink-muted)] opacity-55"
                }`}
              >
                <span className="h-2 w-2 rounded-full bg-[var(--chart-http)]" />
                共享查询 ({httpCalls})
              </button>
              <button
                type="button"
                onClick={() => setShowBars((v) => !v)}
                className={`rounded-[5px] px-2.5 py-1 font-medium ${
                  showBars
                    ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)]"
                    : "bg-transparent text-[var(--ink-muted)] opacity-55"
                }`}
              >
                总量柱图
              </button>
            </div>
          )}

          <button
            type="button"
            onClick={() => setCollapsed((c) => !c)}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)]"
          >
            {collapsed ? (
              <>
                展开统计 <ChevronDown className="h-3.5 w-3.5" />
              </>
            ) : (
              <>
                收起统计 <ChevronUp className="h-3.5 w-3.5" />
              </>
            )}
          </button>
        </div>
      </div>

      {/* Expanded 4-Cell Summary + 24h Trend Chart */}
      {!collapsed && (
        <>
          <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border-subtle)] border-b border-[var(--border-subtle)] lg:grid-cols-4 lg:divide-y-0">
            <div className="px-4 py-3">
              <div className="text-xs font-medium text-[var(--ink-muted)]">平均响应速度</div>
              <div className="mt-1 font-disp text-[26px] leading-none font-extrabold tracking-tight text-[var(--ink-primary)]">
                {p50.toFixed(1)}
                <span className="ml-1 text-xs font-semibold text-[var(--ink-muted)]">ms</span>
              </div>
              <div className="mt-1.5 text-xs text-[var(--ink-muted)]">
                已接入 {vaultCount} 个知识库
              </div>
            </div>

            <div className="px-4 py-3">
              <div className="text-xs font-medium text-[var(--ink-muted)]">较慢查询耗时</div>
              <div className="mt-1 font-disp text-[26px] leading-none font-extrabold tracking-tight text-[var(--chart-http)]">
                {p95.toFixed(1)}
                <span className="ml-1 text-xs font-semibold text-[var(--ink-muted)]">ms</span>
              </div>
              <div className="mt-1.5 text-xs text-[var(--ink-muted)]">95% 的查询快于此速度</div>
            </div>

            <div className="px-4 py-3">
              <div className="flex items-center justify-between text-xs font-medium text-[var(--ink-muted)]">
                <span>近 24 小时查询</span>
                <span className="font-disp text-sm font-bold text-[var(--accent)]">
                  {totalCalls}
                </span>
              </div>
              <svg viewBox="0 0 136 28" className="mt-1.5 h-7 w-full overflow-visible">
                <defs>
                  <linearGradient id="wctx-spark-total" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--chart-stdio)" stopOpacity="0.22" />
                    <stop offset="100%" stopColor="var(--chart-stdio)" stopOpacity="0.01" />
                  </linearGradient>
                </defs>
                <line
                  x1="0"
                  y1="25"
                  x2="136"
                  y2="25"
                  stroke="var(--chart-grid-major)"
                  strokeWidth="1"
                  strokeDasharray="2 2"
                />
                {totalSpark.area && <path d={totalSpark.area} fill="url(#wctx-spark-total)" />}
                {totalSpark.line && (
                  <polyline
                    points={totalSpark.line}
                    fill="none"
                    stroke="var(--chart-stdio)"
                    strokeWidth="1.6"
                  />
                )}
              </svg>
              <div className="mt-1 text-xs text-[var(--ink-muted)]">
                本机 {stdioCalls} · 共享 {httpCalls}
              </div>
            </div>

            <div className="px-4 py-3">
              <div className="flex items-center justify-between text-xs font-medium text-[var(--ink-muted)]">
                <span>权限隐藏次数</span>
                <span className="font-disp text-sm font-bold text-[var(--chart-block)]">
                  {blockedCalls}
                </span>
              </div>
              <svg viewBox="0 0 136 28" className="mt-1.5 h-7 w-full overflow-visible">
                <defs>
                  <linearGradient id="wctx-spark-block" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--chart-block)" stopOpacity="0.22" />
                    <stop offset="100%" stopColor="var(--chart-block)" stopOpacity="0.01" />
                  </linearGradient>
                </defs>
                <line
                  x1="0"
                  y1="25"
                  x2="136"
                  y2="25"
                  stroke="var(--chart-grid-major)"
                  strokeWidth="1"
                  strokeDasharray="2 2"
                />
                {blockedSpark.area && <path d={blockedSpark.area} fill="url(#wctx-spark-block)" />}
                {blockedSpark.line && (
                  <polyline
                    points={blockedSpark.line}
                    fill="none"
                    stroke="var(--chart-block)"
                    strokeWidth="1.6"
                  />
                )}
              </svg>
              <div className="mt-1 text-xs text-[var(--ink-muted)]">
                未命中提问 {zeroHits} 次
              </div>
            </div>
          </div>

          <div className="bg-[var(--chart-plot-bg)] px-4 pt-3 pb-2.5">
            {/* Interactive Hover Readout Strip */}
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
              <div className="flex flex-wrap items-center gap-3 font-mono">
                <span className="rounded bg-[var(--bg-surface)] px-2 py-0.5 font-semibold text-[var(--ink-primary)] ring-1 ring-[var(--border-default)]">
                  {curHourLabel}
                </span>
                <span className="text-[var(--ink-secondary)]">
                  总查询 <strong className="text-[var(--ink-primary)]">{curBin.total}</strong>
                </span>
                <span className="text-[var(--chart-stdio)]">
                  本机 <strong>{curBin.stdio}</strong>
                </span>
                <span className="text-[var(--chart-http)]">
                  共享 <strong>{curBin.http}</strong>
                </span>
                <span className="text-[var(--ink-secondary)]">
                  均速 <strong>{curBin.p50.toFixed(1)}ms</strong>
                </span>
                {curBin.blocked > 0 && (
                  <span className="font-semibold text-[var(--chart-block)]">
                    权限隐藏 {curBin.blocked}
                  </span>
                )}
              </div>
              <span className="text-xs text-[var(--ink-muted)]">移动鼠标查看每小时数据</span>
            </div>

            <div className="relative w-full overflow-hidden">
              <svg
                viewBox={`0 0 ${W} ${H}`}
                className="h-[162px] w-full select-none"
                role="img"
                aria-label="过去 24 小时查询趋势图"
              >
                <defs>
                  <linearGradient id="wctx-grad-stdio" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--chart-stdio)" stopOpacity="0.18" />
                    <stop offset="100%" stopColor="var(--chart-stdio)" stopOpacity="0.0" />
                  </linearGradient>
                  <linearGradient id="wctx-grad-http" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--chart-http)" stopOpacity="0.16" />
                    <stop offset="100%" stopColor="var(--chart-http)" stopOpacity="0.0" />
                  </linearGradient>
                </defs>

                {/* Horizontal Multi-Tier Statistical Gridlines + Left Y-Axis Labels */}
                {[1, 0.75, 0.5, 0.25, 0].map((ratio) => {
                  const y = TOP + (1 - ratio) * plotH;
                  const isBaseline = ratio === 0;
                  const isMajor = ratio === 1 || ratio === 0.5;
                  const valLabel = Math.round(maxY * ratio);
                  return (
                    <g key={ratio}>
                      <line
                        x1={LEFT}
                        y1={y}
                        x2={RIGHT}
                        y2={y}
                        stroke={
                          isBaseline
                            ? "var(--chart-axis)"
                            : isMajor
                              ? "var(--chart-grid-major)"
                              : "var(--chart-grid-minor)"
                        }
                        strokeWidth="1"
                        strokeDasharray={isBaseline ? undefined : isMajor ? "4 4" : "2 4"}
                      />
                      {(isBaseline || isMajor) && (
                        <text
                          x={LEFT - 8}
                          y={y + 4}
                          textAnchor="end"
                          fill="var(--ink-muted)"
                          fontSize="12"
                          fontFamily="var(--font-mono)"
                        >
                          {valLabel}
                        </text>
                      )}
                    </g>
                  );
                })}

                {/* Vertical 6-Hour Cadence Gridlines + Major X-Axis Labels */}
                {majorTicksX.map((tk) => {
                  const x = xAt(tk.idx);
                  return (
                    <g key={tk.idx}>
                      <line
                        x1={x}
                        y1={TOP}
                        x2={x}
                        y2={BOT}
                        stroke="var(--chart-grid-major)"
                        strokeWidth="1"
                        strokeDasharray="3 3"
                      />
                      <line
                        x1={x}
                        y1={BOT}
                        x2={x}
                        y2={BOT + 5}
                        stroke="var(--chart-axis)"
                        strokeWidth="1"
                      />
                      <text
                        x={tk.idx === 0 ? x + 2 : tk.idx === 23 ? x - 2 : x}
                        y={BOT + 20}
                        textAnchor={tk.idx === 0 ? "start" : tk.idx === 23 ? "end" : "middle"}
                        fill="var(--ink-muted)"
                        fontSize="12"
                        fontFamily="var(--font-mono)"
                      >
                        {tk.label}
                      </text>
                    </g>
                  );
                })}

                {/* Minor Hourly Ruler Ticks along X-Axis */}
                {bins.map((_, i) => {
                  const x = xAt(i);
                  return (
                    <line
                      key={`tick-${i}`}
                      x1={x}
                      y1={BOT}
                      x2={x}
                      y2={BOT + 3}
                      stroke="var(--chart-axis)"
                      strokeWidth="1"
                    />
                  );
                })}

                {/* Hourly Volume Columns */}
                {showBars &&
                  bins.map((b, i) => {
                    const barW = Math.max(8, stepX * 0.48);
                    const x = xAt(i) - barW / 2;
                    const y = yAt(b.total);
                    const h = Math.max(b.total > 0 ? 3 : 0, BOT - y);
                    const isFocused = i === safeIdx;
                    return (
                      <rect
                        key={`bar-${i}`}
                        x={x}
                        y={BOT - h}
                        width={barW}
                        height={h}
                        rx="2"
                        fill={isFocused ? "var(--chart-bar-active)" : "var(--chart-bar)"}
                      />
                    );
                  })}

                {/* Active Crosshair Vertical Guide */}
                <line
                  x1={xAt(safeIdx)}
                  y1={TOP}
                  x2={xAt(safeIdx)}
                  y2={BOT}
                  stroke="var(--accent)"
                  strokeOpacity="0.45"
                  strokeWidth="1"
                  strokeDasharray="3 2"
                />

                {/* HTTP Gateway Area + Curve */}
                {showHttp && (
                  <g>
                    {httpGeo.area && <path d={httpGeo.area} fill="url(#wctx-grad-http)" />}
                    {httpGeo.line && (
                      <polyline
                        points={httpGeo.line}
                        fill="none"
                        stroke="var(--chart-http)"
                        strokeWidth="1.8"
                        strokeLinejoin="round"
                        strokeLinecap="round"
                      />
                    )}
                    <circle
                      cx={xAt(safeIdx)}
                      cy={yAt(curBin.http)}
                      r="4"
                      fill="var(--chart-http)"
                      stroke="var(--bg-surface)"
                      strokeWidth="1.8"
                    />
                  </g>
                )}

                {/* stdio Local Area + Curve */}
                {showStdio && (
                  <g>
                    {stdioGeo.area && <path d={stdioGeo.area} fill="url(#wctx-grad-stdio)" />}
                    {stdioGeo.line && (
                      <polyline
                        points={stdioGeo.line}
                        fill="none"
                        stroke="var(--chart-stdio)"
                        strokeWidth="1.9"
                        strokeLinejoin="round"
                        strokeLinecap="round"
                      />
                    )}
                    {bins.map((b, i) =>
                      b.stdio > 0 && i !== safeIdx ? (
                        <circle
                          key={`pt-s-${i}`}
                          cx={xAt(i)}
                          cy={yAt(b.stdio)}
                          r="2.2"
                          fill="var(--chart-stdio)"
                        />
                      ) : null,
                    )}
                    <circle
                      cx={xAt(safeIdx)}
                      cy={yAt(curBin.stdio)}
                      r="4.2"
                      fill="var(--chart-stdio)"
                      stroke="var(--bg-surface)"
                      strokeWidth="1.8"
                    />
                  </g>
                )}

                {/* ACL Blocked Warning Markers */}
                {bins.map((b, i) =>
                  b.blocked > 0 ? (
                    <circle
                      key={`blk-${i}`}
                      cx={xAt(i)}
                      cy={Math.max(TOP + 5, yAt(b.total) - 7)}
                      r="3.2"
                      fill="var(--chart-block)"
                      stroke="var(--bg-surface)"
                      strokeWidth="1.2"
                    />
                  ) : null,
                )}

                {/* Invisible Full-Height Hit Targets for Smooth Hover Inspection */}
                {bins.map((_, i) => (
                  <rect
                    key={`hit-${i}`}
                    x={xAt(i) - stepX / 2}
                    y={TOP}
                    width={stepX}
                    height={plotH + 22}
                    fill="transparent"
                    className="cursor-crosshair"
                    onMouseEnter={() => setActiveIdx(i)}
                    onClick={() => setActiveIdx(i)}
                  />
                ))}
              </svg>
            </div>
          </div>
        </>
      )}
    </section>
  );
};
