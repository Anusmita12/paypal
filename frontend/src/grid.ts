import { AllCommunityModule, ModuleRegistry, themeQuartz } from "ag-grid-community";
import type { ProcessCellForExportParams } from "ag-grid-community";

// Community modules only: no ag-grid-enterprise, no license key.
ModuleRegistry.registerModules([AllCommunityModule]);

export const gridTheme = themeQuartz.withParams({
  accentColor: "#1f5eff",
  borderRadius: 8,
  wrapperBorderRadius: 12,
  headerFontWeight: 600,
  fontFamily: "inherit",
  fontSize: 13,
  rowHeight: 38,
  headerHeight: 40,
  backgroundColor: "var(--grid-bg)",
  foregroundColor: "var(--text)",
  headerBackgroundColor: "var(--grid-header)",
  borderColor: "var(--border)",
  oddRowBackgroundColor: "var(--grid-odd)",
  selectedRowBackgroundColor: "var(--accent-soft)",
});

/** Spreadsheet-formula neutralization for CSV export. */
export function safeCsvCell(params: ProcessCellForExportParams): string {
  const v = params.value == null ? "" : String(params.value);
  return /^[=+\-@\t\r]/.test(v) ? `'${v}` : v;
}
