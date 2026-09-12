const UNITS = ["B", "KB", "MB", "GB", "TB", "PB", "EB"] as const;

/** Auto-scales to the largest unit where the value is still >= 1, instead
 * of always dividing by 1e9 -- a sentinel/overflow value (e.g. an int64
 * max reported as disk_free_bytes) rendered as GB shows an absurd
 * "9223372036.8 GB"; scaled, the same value reads as "9.2 EB", which at
 * least signals "this is not a real disk size" instead of a wall of
 * digits. Decimal (1000-based), matching this dashboard's existing "GB"
 * labels -- not binary GiB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) {
    return "—";
  }
  const sign = bytes < 0 ? "-" : "";
  let value = Math.abs(bytes);
  let unitIndex = 0;
  while (value >= 1000 && unitIndex < UNITS.length - 1) {
    value /= 1000;
    unitIndex++;
  }
  const decimals = unitIndex === 0 ? 0 : 1;
  return `${sign}${value.toFixed(decimals)} ${UNITS[unitIndex]}`;
}

/** A latency in milliseconds, as a dashboard reads it: sub-second numbers
 * stay in ms (that is the unit a voice deployment tunes in), a second or
 * more reads as seconds. `null`/`undefined` is "not recorded" -- a journey
 * from before schema 2.1, or an integration that cannot time its provider --
 * and shows the same em dash every other unset value here does. */
export function formatMs(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) {
    return "—";
  }
  if (Math.abs(ms) < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return `${(ms / 1000).toFixed(2)} s`;
}
