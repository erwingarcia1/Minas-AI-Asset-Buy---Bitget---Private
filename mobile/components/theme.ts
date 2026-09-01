export const colors = {
  bg: "#F5F7F6", surface: "#FFFFFF", ink: "#1C2422", muted: "#5A6763",
  accent: "#0E6E62", danger: "#B3392F", warn: "#946608", line: "#DCE3E0",
};
export const card = {
  backgroundColor: colors.surface, borderRadius: 10, padding: 16,
  marginBottom: 12,
} as const;
export const input = {
  borderWidth: 1, borderColor: colors.line, borderRadius: 8,
  padding: 12, marginTop: 4, backgroundColor: colors.surface,
} as const;
