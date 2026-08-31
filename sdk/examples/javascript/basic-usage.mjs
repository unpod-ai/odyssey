// @odyssey/sdk — a runnable walkthrough of every resource.
//
// Prerequisites: a real services/api instance reachable at the URL below
// (see sdk/examples/README.md), and @odyssey/sdk built (pnpm --filter
// @odyssey/sdk build) since this imports its dist/ output directly.
//
// Run from the repo root:
//
//   node sdk/examples/javascript/basic-usage.mjs [baseUrl]

import { OdysseySDK, OdysseyAPINotFoundError } from "../../javascript/dist/index.js";

async function main() {
  const baseUrl = process.argv[2] ?? "http://127.0.0.1:8000";
  const client = new OdysseySDK(baseUrl);

  console.log("health:", await client.health());

  const journeys = await client.journeys.list();
  console.log(`${journeys.length} journey(s)`);
  for (const j of journeys.slice(0, 3)) console.log(" -", j.journey_id);

  if (journeys.length > 0) {
    const detail = await client.journeys.get(journeys[0].journey_id);
    console.log("first journey detail:", detail.journey_id, "complete =", detail.complete);
  }

  try {
    await client.journeys.get("does-not-exist");
  } catch (err) {
    if (err instanceof OdysseyAPINotFoundError) {
      console.log("journeys.get('does-not-exist') -> 404, as expected");
    } else {
      throw err;
    }
  }

  console.log("datasets:", (await client.datasets.list()).map((d) => d.name));
  console.log("models:", (await client.models.list()).map((m) => m.name));
  console.log("eval runs:", (await client.runs.list()).map((r) => r.benchmark_name));
  console.log("exports:", (await client.exports.list()).map((e) => e.name));
}

main();
