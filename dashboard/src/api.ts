import { ApiError } from "./errors";
let csrf = "";
let bootstrap: Promise<void> | undefined;
export function initialize(): Promise<void> {
  bootstrap ??= fetch("/api/bootstrap")
    .then(async (response) => {
      if (!response.ok) throw new Error("无法连接本机管理服务");
      csrf = (await response.json()).csrf;
    })
    .catch((error) => {
      bootstrap = undefined;
      throw error instanceof ApiError ? error : new ApiError(error);
    });
  return bootstrap;
}
export async function api<T>(
  path: string,
  body?: Record<string, unknown>,
  method = "POST",
): Promise<T> {
  try {
  await initialize();
  const response = await fetch(
    `/api${path}`,
    body
      ? {
          method,
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({
            ...body,
            operation_id: body.operation_id ?? crypto.randomUUID(),
          }),
        }
      : undefined,
  );
  const data = await response.json();
  if (response.status === 401) {
    bootstrap = undefined;
    csrf = "";
  }
  if (!response.ok)
    throw new ApiError(data.error ? data : {error:`HTTP ${response.status}`});
  return data;
  } catch(error) { throw error instanceof ApiError ? error : new ApiError(error); }
}
