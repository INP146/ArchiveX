const compactCountFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1
});
const groupedIntegerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0
});

export function formatCount(value: number | null) {
  return compactCountFormatter.formatToParts(value ?? 0)
    .map((part) => part.type === "integer"
      ? groupedIntegerFormatter.format(Number(part.value))
      : part.value)
    .join("");
}
