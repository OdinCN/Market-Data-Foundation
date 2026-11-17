// Test-only ASGI transport. Python fixtures prohibit sockets and external stores.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import type { Transport } from "./api";
const children: ChildProcessWithoutNullStreams[] = [];
export function closeBridges() {
  children.splice(0).forEach((c) => {
    c.stdin.end();
    c.kill();
  });
}
export function asgi(
  scenario = "unconfigured",
  script = "r5_ui_exchange.py",
  fields: Record<string, string> = {},
) {
  const root = resolve(process.cwd(), "..");
  const child = spawn(
    `${root}/.venv/bin/python`,
    ["-u", `tests/${script}`, scenario],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPATH: `${root}/src:${root}/tests:${root}:${process.env.PYTHONPATH ?? ""}`,
      },
    },
  );
  children.push(child);
  const pending: {
    resolve: (data: any) => void;
    reject: (e: Error) => void;
  }[] = [];
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += String(chunk);
  });
  createInterface({ input: child.stdout }).on("line", (line) =>
    pending.shift()?.resolve(JSON.parse(line)),
  );
  child.on("exit", (code) =>
    pending
      .splice(0)
      .forEach((p) => p.reject(new Error(`ASGI fixture ${code}: ${stderr}`))),
  );
  const effects: {
    source: number;
    raw: number;
    jobs: number;
    versions?: number;
    publications?: number;
  } = { source: 0, raw: 0, jobs: 0 };
  async function exchange(message: object) {
    const result = await new Promise<any>((resolve, reject) => {
      pending.push({ resolve, reject });
      child.stdin.write(
        JSON.stringify({
          ...fields,
          ...(["d01_ui_exchange.py", "d02_ui_exchange.py"].includes(script)
            ? { scenario }
            : {}),
          ...message,
        }) + "\n",
      );
    });
    if (result.effects) Object.assign(effects, result.effects);
    return result;
  }
  const requests: {
    path: string;
    method: string;
    body?: any;
    status?: number;
  }[] = [];
  let lose = false;
  const transport: Transport = async (input, init) => {
    const req = {
      path: String(input),
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers).has("Prefer")
        ? { Prefer: new Headers(init?.headers).get("Prefer") }
        : {},
      ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}),
    };
    requests.push(req);
    const r = await exchange(req);
    Object.assign(req, { status: r.status });
    if (lose && ["POST", "PUT"].includes(req.method)) {
      lose = false;
      throw new Error("synthetic_lost_receipt");
    }
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body,
    } as Response;
  };
  return {
    transport,
    requests,
    effects,
    exchange,
    loseNext: () => {
      lose = true;
    },
  };
}
