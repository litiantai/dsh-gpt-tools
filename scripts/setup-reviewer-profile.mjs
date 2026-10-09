// Provision an isolated base/headless profile while retaining the source profile's model routes.
import {
  readFile,
  writeFile,
  mkdir,
  lstat,
  symlink,
  access,
} from "node:fs/promises";
import { resolve, join } from "node:path";
import { parseDocument, Document, YAMLSeq } from "yaml";
const [home, sourceName, targetName, runtime] = process.argv.slice(2);
if (
  !home ||
  !sourceName ||
  !targetName ||
  !runtime ||
  sourceName === targetName
)
  throw new Error(
    "Usage: setup-reviewer-profile HOME SOURCE_PROFILE REVIEW_PROFILE HARNESS_ROOT",
  );
const target = join(resolve(home), "profiles", targetName);
try {
  await lstat(target);
  throw new Error("目标 profile 已存在，请核对后更新，禁止覆盖");
} catch (e) {
  if (e.code !== "ENOENT") throw e;
}
let entry;
for (const candidate of [
  "apps/cli/lib/bin.js",
  "node_modules/@deepseek-ai/dsh/lib/bin.js",
  "lib/bin.js",
]) {
  try {
    await access(join(resolve(runtime), candidate));
    entry = join(resolve(runtime), candidate);
    break;
  } catch {}
}
if (!entry)
  throw new Error("找不到已构建的 Harness CLI；不能使用未编译源码代替");
const source = join(resolve(home), "profiles", sourceName);
const doc = parseDocument(
  await readFile(join(source, "cordis.patch.yml"), "utf8"),
);
if (doc.errors.length) throw new Error("来源 profile YAML 无法解析");
const out = new Document();
out.contents = new YAMLSeq();
for (const item of doc.contents?.items || []) {
  const id = item.get?.("id");
  if (
    typeof id === "string" &&
    (id.startsWith("llm-") || id === "agent-default-model")
  )
    out.contents.items.push(item.clone());
}
await mkdir(target, { recursive: true, mode: 0o700 });
await writeFile(
  join(target, "package.json"),
  JSON.stringify(
    {
      name: "dsh-supervisor-review-profile",
      private: true,
      dsh: {
        profile: {
          bundles: ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"],
          patchReload: "none",
        },
      },
    },
    null,
    2,
  ),
  { mode: 0o600 },
);
await writeFile(join(target, "cordis.patch.yml"), String(out), { mode: 0o600 });
await symlink(join(source, "node_modules"), join(target, "node_modules"));
const launcher = join(resolve(home), "supervisor", "bin", "dsh-review.mjs");
await mkdir(join(resolve(home), "supervisor", "bin"), {
  recursive: true,
  mode: 0o700,
});
await writeFile(
  launcher,
  '#!/usr/bin/env node\nimport {spawn} from "node:child_process";\nconst child=spawn(' +
    JSON.stringify(process.execPath) +
    ",[" +
    JSON.stringify(entry) +
    ',...process.argv.slice(2)],{stdio:"inherit",env:process.env});\nfor(const sig of ["SIGTERM","SIGINT"])process.on(sig,()=>child.kill(sig));\nchild.on("error",()=>process.exit(1)); child.on("exit",code=>process.exit(code??1));\n',
  { mode: 0o700, flag: "wx" },
);
console.log(
  JSON.stringify({
    profile: target,
    modelOverrides: out.contents.items.length,
    harness_bin: launcher,
  }),
);
