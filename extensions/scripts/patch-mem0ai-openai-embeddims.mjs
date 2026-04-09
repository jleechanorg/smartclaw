#!/usr/bin/env node
/**
 * mem0ai OSS OpenAIEmbedder reads embeddingDims but (as of 2.2.1) does not pass
 * `dimensions` to OpenAI embeddings.create — so text-embedding-3-small returns
 * 1536-d vectors while Qdrant collections are often 768 (per openclaw.json).
 * Upstream: re-check after mem0ai upgrades; remove if fixed.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** @param {string} source */
export function patchMem0aiOpenAiEmbedCalls(source) {
  if (source.includes("dimensions: this.embeddingDims")) {
    return source;
  }
  const a = `const response = await this.openai.embeddings.create({
      model: this.model,
      input: text
    });`;
  const b = `const response = await this.openai.embeddings.create({
      model: this.model,
      input: text,
      dimensions: this.embeddingDims
    });`;
  const c = `const response = await this.openai.embeddings.create({
      model: this.model,
      input: texts
    });`;
  const d = `const response = await this.openai.embeddings.create({
      model: this.model,
      input: texts,
      dimensions: this.embeddingDims
    });`;
  if (!source.includes(a)) {
    throw new Error(
      "patch-mem0ai: mem0ai OpenAIEmbedder embed() pattern not found; mem0ai version may have changed",
    );
  }
  let out = source.replace(a, b);
  if (out.includes(c)) {
    out = out.replace(c, d);
  }
  return out;
}

function patchFile(relPath) {
  const abs = path.join(__dirname, "..", relPath);
  if (!fs.existsSync(abs)) {
    return { relPath, skipped: true, reason: "missing" };
  }
  const before = fs.readFileSync(abs, "utf8");
  if (before.includes("dimensions: this.embeddingDims")) {
    return { relPath, skipped: true, reason: "already_patched" };
  }
  const after = patchMem0aiOpenAiEmbedCalls(before);
  fs.writeFileSync(abs, after, "utf8");
  return { relPath, skipped: false };
}

function main() {
  const targets = [
    "node_modules/mem0ai/dist/oss/index.mjs",
    "node_modules/mem0ai/dist/oss/index.js",
  ];
  const results = [];
  for (const rel of targets) {
    try {
      results.push(patchFile(rel));
    } catch (e) {
      console.error(`patch-mem0ai: failed for ${rel}:`, e);
      process.exit(1);
    }
  }
  const applied = results.filter((r) => !r.skipped);
  const skipped = results.filter((r) => r.skipped);
  if (applied.length) {
    console.log(
      `patch-mem0ai: applied OpenAI dimensions fix to ${applied.map((r) => r.relPath).join(", ")}`,
    );
  }
  for (const s of skipped) {
    console.log(`patch-mem0ai: skip ${s.relPath} (${s.reason})`);
  }
}

const isMain =
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  main();
}
