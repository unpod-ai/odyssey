/**
 * Against a real `services/api` instance (started via `uv run odyssey api
 * serve` as a child process), not a mocked `fetch` — the same convention
 * `sdk/python/tests/test_client.py` uses for this exact package's Python
 * twin.
 */
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { OdysseyAPINotFoundError, OdysseySDK } from "../src/index.js";

const REPO_ROOT = join(import.meta.dirname, "..", "..", "..");
const JID = "j_sdk_js";

async function freePort(): Promise<number> {
  const net = await import("node:net");
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address && typeof address === "object") {
        const port = address.port;
        server.close(() => resolve(port));
      } else {
        server.close(() => reject(new Error("could not allocate a free port")));
      }
    });
  });
}

async function waitForHealth(baseUrl: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    try {
      const res = await fetch(`${baseUrl}/health`);
      if (res.ok) return;
    } catch {
      // server not up yet
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error("server did not start in time");
}

let baseUrl: string;
let serverProcess: ChildProcess;
let journeysDir: string;

beforeAll(async () => {
  const tmp = mkdtempSync(join(tmpdir(), "odyssey-sdk-js-"));
  journeysDir = join(tmp, "journeys");
  const dateDir = join(journeysDir, "2026-08-31");

  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import sys
sys.path.insert(0, ".")
from pathlib import Path
from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal

date_dir = Path(${JSON.stringify(dateDir)})
date_dir.mkdir(parents=True, exist_ok=True)
write_events(
    date_dir / "${JID}.jsonl",
    [
        JourneyEvent(journey_id="${JID}", seq=0, kind="message", event_id="e0", message=Message(role="user", content="hi")),
        JourneyEvent(journey_id="${JID}", seq=1, kind="terminal", event_id="e1", terminal=Terminal(termination_reason="ENV_DONE")),
    ],
    header=JourneyHeader(journey_id="${JID}", data_source="livekit"),
)
`,
    ],
    { cwd: REPO_ROOT, stdio: "inherit" },
  );

  const port = await freePort();
  baseUrl = `http://127.0.0.1:${port}`;
  serverProcess = spawn(
    "uv",
    ["run", "odyssey", "api", "serve", "--port", String(port)],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        ODYSSEY_API_JOURNEYS_DIR: journeysDir,
        ODYSSEY_API_DATASETS_REGISTRY: join(tmp, "no-such-datasets.yaml"),
        ODYSSEY_API_MODELS_REGISTRY: join(tmp, "no-such-models.yaml"),
        ODYSSEY_API_EVAL_REGISTRY: join(tmp, "no-such-eval.yaml"),
        ODYSSEY_API_EVAL_REPORTS_DIR: join(tmp, "no-such-reports"),
        ODYSSEY_API_EXPORTS_DIR: join(tmp, "no-such-exports"),
      },
      stdio: "ignore",
    },
  );
  await waitForHealth(baseUrl);
}, 60_000);

afterAll(() => {
  serverProcess?.kill();
});

describe("OdysseySDK against a real services/api instance", () => {
  test("health", async () => {
    const client = new OdysseySDK(baseUrl);
    expect((await client.health()).status).toBe("ok");
  });

  test("journeys list and get", async () => {
    const client = new OdysseySDK(baseUrl);
    const listed = await client.journeys.list();
    expect(listed.map((j) => j.journey_id)).toEqual([JID]);

    const detail = await client.journeys.get(JID);
    expect(detail.complete).toBe(true);
    expect(detail.steps.length).toBeGreaterThan(0);
  });

  test("journeys get missing raises not found", async () => {
    const client = new OdysseySDK(baseUrl);
    await expect(client.journeys.get("does-not-exist")).rejects.toBeInstanceOf(
      OdysseyAPINotFoundError,
    );
  });

  test("empty registries return empty lists", async () => {
    const client = new OdysseySDK(baseUrl);
    expect(await client.datasets.list()).toEqual([]);
    expect(await client.models.list()).toEqual([]);
    expect(await client.runs.list()).toEqual([]);
    expect(await client.exports.list()).toEqual([]);
  });
});
