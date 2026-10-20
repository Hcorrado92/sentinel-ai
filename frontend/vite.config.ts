/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Vite and Vitest print symbols such as "✓" and "❯". When output is captured
// (not a TTY) by tools that decode it with a legacy console code page, those
// characters break them, so we transliterate non-ASCII output to ASCII.
const ASCII_REPLACEMENTS: Record<string, string> = {
  "✓": "+",
  "✔": "+",
  "×": "x",
  "✗": "x",
  "✘": "x",
  "❯": ">",
  "→": "->",
  "↓": "v",
  "·": ".",
  "…": "...",
  "─": "-",
  "│": "|",
};

function toAscii(text: string): string {
  return text.replace(/[^\x00-\x7F]/g, (ch) => ASCII_REPLACEMENTS[ch] ?? "?");
}

function forceAsciiOutput(stream: NodeJS.WriteStream): void {
  if (stream.isTTY) return;
  const write = stream.write.bind(stream) as (...args: unknown[]) => boolean;
  stream.write = ((chunk: unknown, ...rest: unknown[]) => {
    if (typeof chunk === "string") return write(toAscii(chunk), ...rest);
    if (chunk instanceof Uint8Array) return write(toAscii(Buffer.from(chunk).toString("utf8")), ...rest);
    return write(chunk, ...rest);
  }) as typeof stream.write;
}

forceAsciiOutput(process.stdout);
forceAsciiOutput(process.stderr);

export default defineConfig(({ command }) => ({
  // The production build reporter prints "✓ N modules transformed" through a
  // path the ASCII shim above does not always intercept, so builds only log
  // errors. The dev server keeps the default log level.
  logLevel: command === "build" ? "error" : "info",
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/setupTests.ts"],
  },
}));
