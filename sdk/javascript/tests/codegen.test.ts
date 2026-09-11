/**
 * Against the real, committed `services/api/openapi.json` — not a
 * synthetic schema, so a real drift is what this actually catches.
 */
import { describe, expect, test } from "vitest";
import {
  UnsupportedOperationError,
  checkDrift,
  loadOpenapi,
  operationsByResource,
  renderAll,
} from "../src/codegen.js";

function getOp(responseRef = "#/components/schemas/Widget") {
  return {
    get: {
      responses: {
        "200": { content: { "application/json": { schema: { $ref: responseRef } } } },
      },
    },
  };
}

describe("codegen", () => {
  test("distinct last segments get distinct method names", () => {
    const openapi = {
      paths: {
        "/widgets": getOp(),
        "/widgets/counts": getOp(),
        "/widgets/totals": getOp(),
      },
      components: { schemas: {} },
    } as any;
    const byResource = operationsByResource(openapi);
    const methodNames = new Set(byResource.widgets.map((op) => op.methodName));
    expect(methodNames).toEqual(new Set(["list", "counts", "totals"]));
  });

  test("colliding last segments raise instead of clobbering", () => {
    const openapi = {
      paths: {
        // Two different sub-paths on the *same* resource that happen to
        // derive the same last-segment method name -- the exact collision
        // the duplicate-name guard exists to catch.
        "/widgets/a/counts": getOp(),
        "/widgets/b/counts": getOp(),
      },
      components: { schemas: {} },
    } as any;
    expect(() => operationsByResource(openapi)).toThrow(UnsupportedOperationError);
  });

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
      "products",
      "runs",
    ]);
    expect(resources.journeys).toContain("class JourneysResource");
  });

  test("no drift against committed resources", () => {
    expect(checkDrift()).toEqual([]);
  });
});
