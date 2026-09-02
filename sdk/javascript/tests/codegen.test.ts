/**
 * Against the real, committed `services/api/openapi.json` — not a
 * synthetic schema, so a real drift is what this actually catches.
 */
import { describe, expect, test } from "vitest";
import { checkDrift, loadOpenapi, renderAll } from "../src/codegen.js";

describe("codegen", () => {
  test("committed openapi is the real narrow shape", () => {
    const openapi = loadOpenapi();
    expect(openapi.paths["/journeys"]).toBeDefined();
    expect(openapi.paths["/health"]).toBeDefined();
  });

  test("renderAll produces one module per resource", () => {
    const { resources } = renderAll(loadOpenapi());
    expect(Object.keys(resources).sort()).toEqual([
      "datasets",
      "exports",
      "journeys",
      "metrics",
      "models",
      "runs",
    ]);
    expect(resources.journeys).toContain("class JourneysResource");
  });

  test("no drift against committed resources", () => {
    expect(checkDrift()).toEqual([]);
  });
});
