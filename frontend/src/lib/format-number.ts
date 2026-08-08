const countFormatter = new Intl.NumberFormat("en-US");

export function formatCount(value: number | null) {
  return countFormatter.format(value ?? 0);
}
