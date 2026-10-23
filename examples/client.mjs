// Example client for Node.js 18+ (native fetch, FormData and Blob, no dependencies).
// It first tries POST /jobs?wait=30 (PDF in a single call); if more time is needed it polls.
//
// Usage:
//   FOLDPDF_API_KEY=... node examples/client.mjs report.docx [http://127.0.0.1:8080]

import { readFile, writeFile } from "node:fs/promises";
import { basename, extname } from "node:path";

const [file, baseUrl = "http://127.0.0.1:8080"] = process.argv.slice(2);
if (!file) {
  console.error("Usage: node examples/client.mjs <file.docx|xlsx|pptx> [url]");
  process.exit(2);
}
const apiKey = process.env.FOLDPDF_API_KEY;
const headers = apiKey ? { "X-API-Key": apiKey } : {};
const output = file.slice(0, file.length - extname(file).length) + ".pdf";

async function call(path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, { ...options, headers: { ...headers, ...options.headers } });
  if (!response.ok && response.status !== 202) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response;
}

const form = new FormData();
form.append("file", new Blob([await readFile(file)]), basename(file));
const first = await call("/jobs?wait=30", { method: "POST", body: form });

if (first.status === 200) {
  await writeFile(output, Buffer.from(await first.arrayBuffer()));
  console.log(`PDF saved to ${output} (job ${first.headers.get("x-job-id")}, single call)`);
  process.exit(0);
}

let job = await first.json();
console.log(`Job ${job.job_id} still running (${job.status}): polling...`);
while (job.status === "queued" || job.status === "processing") {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  job = await (await call(job.status_url)).json();
}
if (job.status === "failed") {
  console.error(`Conversion failed: ${job.error}`);
  process.exit(1);
}
await writeFile(output, Buffer.from(await (await call(job.download_url)).arrayBuffer()));
console.log(`PDF saved to ${output}`);
