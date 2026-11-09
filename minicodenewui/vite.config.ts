import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// 单文件产物：dist/index.html 可以直接双击打开，不需要起服务。
export default defineConfig({
  base: "./",
  plugins: [react(), viteSingleFile()],
  build: {
    assetsInlineLimit: 100_000_000,
    chunkSizeWarningLimit: 8000,
  },
});
