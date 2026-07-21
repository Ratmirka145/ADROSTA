import process from "node:process";
import { createApp } from "./app.js";

try {
  process.loadEnvFile?.();
} catch (error) {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String(error.code)
      : "";

  if (code !== "ENOENT") {
    throw error;
  }
}

const port = Number(process.env.PORT ?? 3000);
const host = "0.0.0.0";

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("Переменная PORT должна содержать корректный номер порта");
}

const app = createApp();

app.listen(port, host, () => {
  console.log(`ADROSTA backend запущен: http://${host}:${port}`);
});
