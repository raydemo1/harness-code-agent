export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function clipboardFilePaths(text: string): string[] {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    if (!line.toLowerCase().startsWith("file:///")) return line;
    return decodeURIComponent(line.slice("file:///".length)).replace(/^\/([A-Za-z]:)/, "$1").replaceAll("/", "\\");
  }).filter((line) => /^[A-Za-z]:\\/.test(line) || /^\\\\/.test(line));
}
