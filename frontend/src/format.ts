const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** Integer cents to a display string. Arithmetic never happens in floats on the client. */
export const money = (cents: number | null | undefined) => (cents == null ? "—" : usd.format(cents / 100));

export const cents = (cents: number) => `${cents}¢`;

export const pct = (bps: number) => `${(bps / 100).toFixed(bps % 100 ? 2 : 0)}%`;

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function day(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function minutesLeft(iso: string, now = Date.now()): number {
  return Math.floor((new Date(iso).getTime() - now) / 60000);
}

export const humanize = (code: string) => code.replace(/_/g, " ");
