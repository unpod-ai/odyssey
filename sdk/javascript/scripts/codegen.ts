import { generate, checkDrift } from "../src/codegen.js";

const check = process.argv.includes("--check");

if (check) {
  const drifted = checkDrift();
  if (drifted.length > 0) {
    console.log(`stale: ${drifted.join(", ")} — run \`pnpm --filter @odyssey/sdk codegen\``);
    process.exit(3);
  }
  console.log("sdk/javascript: fresh");
} else {
  for (const path of generate()) {
    console.log(`wrote ${path}`);
  }
}
