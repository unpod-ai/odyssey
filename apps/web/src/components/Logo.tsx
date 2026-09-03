/** The orbit/waypoint mark — see docs/assets/brand/logo-mark.svg (source of
 * truth for the design; this is the same paths, inlined so the sidebar
 * doesn't pay a network round-trip for a 12x12 icon). */
export function LogoMark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="odyssey-mark-grad" x1="6" y1="6" x2="42" y2="42" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#4f46e5" />
          <stop offset="1" stopColor="#14e8c4" />
        </linearGradient>
      </defs>
      <circle
        cx="24"
        cy="24"
        r="16"
        fill="none"
        stroke="url(#odyssey-mark-grad)"
        strokeWidth="5"
        strokeLinecap="round"
        strokeDasharray="23.51 10"
        transform="rotate(-90 24 24)"
      />
      <circle cx="24" cy="8" r="4" fill="url(#odyssey-mark-grad)" />
    </svg>
  );
}
