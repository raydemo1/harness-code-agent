import type { ThemePreference } from "./protocol.ts";

export type ThemeMode = "dark" | "light";
export type Theme = {
  mode: ThemeMode; background: string; surface: string; surfaceRaised: string; surfaceSelected: string;
  text: string; muted: string; subtle: string; accent: string; accentSoft: string; focus: string;
  success: string; warning: string; error: string; border: string; diffAdd: string; diffDelete: string;
};

export const themes: Record<ThemeMode, Theme> = {
  dark: {
    mode: "dark", background: "#0f1214", surface: "#171b1e", surfaceRaised: "#1d2327", surfaceSelected: "#273139",
    text: "#eef1f2", muted: "#b6c0c5", subtle: "#7e8a91", accent: "#72c7e8", accentSoft: "#24424f",
    focus: "#9bdcf4", success: "#88d8a0", warning: "#e5c778", error: "#ef9a9a", border: "#303b41",
    diffAdd: "#7fd39a", diffDelete: "#ec9090",
  },
  light: {
    mode: "light", background: "#f6f8f9", surface: "#edf1f3", surfaceRaised: "#e4eaed", surfaceSelected: "#d8e5eb",
    text: "#172126", muted: "#46575f", subtle: "#5e6f77", accent: "#087da6", accentSoft: "#cfe8f1",
    focus: "#056887", success: "#237a43", warning: "#8a6500", error: "#b33a3a", border: "#c8d2d7",
    diffAdd: "#257a44", diffDelete: "#b23d3d",
  },
};

export function resolveTheme(preference: ThemePreference, detected: ThemeMode | null): Theme {
  if (preference === "dark" || preference === "light") return themes[preference];
  return themes[detected ?? "dark"];
}
