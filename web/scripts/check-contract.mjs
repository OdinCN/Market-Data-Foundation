import { readFile } from "node:fs/promises";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";
import { format } from "prettier";

const schema = new URL("../openapi.json", import.meta.url);
const target = new URL("../src/api.generated.ts", import.meta.url);
const generated = await format(
  COMMENT_HEADER + astToString(await openapiTS(schema)),
  { parser: "typescript" },
);
if ((await readFile(target, "utf8")) !== generated) {
  throw new Error(
    "Generated TypeScript contract is stale. Run npm run contract.",
  );
}
console.log("OpenAPI → TypeScript contract is current.");
