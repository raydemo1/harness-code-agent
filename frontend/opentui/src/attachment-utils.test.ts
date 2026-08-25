import { describe, expect, test } from "bun:test";
import { clipboardFilePaths, formatBytes } from "./attachment-utils.ts";

describe("attachment utilities", () => {
  test("parses Explorer-style URI lists", () => {
    expect(clipboardFilePaths("file:///C:/Work/a.png\r\nfile:///C:/Work/spec.pdf\r\n")).toEqual([
      "C:\\Work\\a.png",
      "C:\\Work\\spec.pdf",
    ]);
  });

  test("does not interpret ordinary clipboard prose as a file list", () => {
    expect(clipboardFilePaths("please inspect the current implementation")).toEqual([]);
  });

  test("formats attachment sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
  });
});
