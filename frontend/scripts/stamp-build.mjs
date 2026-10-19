// Writes dist/BUILD_INFO after `vite build`, so scripts/deploy.sh can refuse to
// serve a bundle that was not built from the checkout being deployed.
// frontend_tree is the git tree hash of frontend/ (backend-only commits do not
// change it, so a bundle stays valid until the frontend itself changes).
import { execSync } from "node:child_process";
import { writeFileSync, existsSync } from "node:fs";

const git = (args) => { try { return execSync(`git ${args}`, { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); } catch { return "unknown"; } };
if (!existsSync("dist/index.html")) { console.error("stamp-build: dist/index.html missing, run vite build first"); process.exit(1); }
const info = [`commit=${git("rev-parse HEAD")}`, `frontend_tree=${git("rev-parse HEAD:frontend")}`,
              `dirty=${git("status --porcelain -- .") ? "yes" : "no"}`, `built_at=${new Date().toISOString()}`];
writeFileSync("dist/BUILD_INFO", info.join("\n") + "\n");
console.log("stamp-build: " + info.join(" "));
