import { readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";
import { format } from "prettier";

for (const [source, output] of [
  ["../openapi.json", "../src/api.generated.ts"],
  ["../src/retail.openapi.json", "../src/retail.generated.ts"],
  [
    "../src/macro-timeseries.openapi.json",
    "../src/macro-timeseries.generated.ts",
  ],
  ["../src/management.openapi.json", "../src/management.generated.ts"],
]) {
  const target = new URL(output, import.meta.url);
  const generated = await format(
    COMMENT_HEADER +
      astToString(await openapiTS(new URL(source, import.meta.url))),
    { parser: "typescript" },
  );
  if (process.argv.includes("--write")) await writeFile(target, generated);
  else if ((await readFile(target, "utf8")) !== generated) {
    throw new Error(
      "Generated TypeScript contract is stale. Run node scripts/check-contract.mjs --write.",
    );
  }
}
console.log("OpenAPI → TypeScript contract is current.");
