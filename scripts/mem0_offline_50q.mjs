#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

function utcStamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function listMarkdownFiles(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...listMarkdownFiles(p));
    else if (entry.isFile() && entry.name.endsWith(".md")) out.push(p);
  }
  return out;
}

function buildPairMap(text) {
  const map = new Map();
  const re1 = /\*([A-Z0-9\-e2]+)\*.*?`(ai-orch-\d+)`/g;
  const re2 = /(ORCH-[a-z0-9\-]+).*?`(ai-orch-\d+)`/g;
  for (const m of text.matchAll(re1)) {
    if (m[1].startsWith("ORCH-")) map.set(m[1], m[2]);
  }
  for (const m of text.matchAll(re2)) {
    if (m[1].startsWith("ORCH-")) map.set(m[1], m[2]);
  }
  return map;
}

function buildExpected(pairs) {
  const expected = [];
  const sorted = [...pairs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  for (const [orch, branch] of sorted.slice(0, 30)) {
    expected.push({
      question: `Which branch was ${orch} committed to?`,
      must_contain: [branch],
      kind: "orch_to_branch",
      orch,
      branch,
    });
  }
  const canon = [
    ["ORCH-e2e-029c50", "ai-orch-56066"],
    ["ORCH-e2e-2cfd73", "ai-orch-55438"],
    ["ORCH-self-hosted-runner-001", "ai-orch-92020"],
  ];
  for (const [orch, branch] of canon) {
    expected.push({
      question: `Which branch was ${orch} committed to?`,
      must_contain: [branch],
      kind: "orch_to_branch",
      orch,
      branch,
    });
  }
  for (const [orch, branch] of sorted.slice(0, 20)) {
    expected.push({
      question: `Find the ORCH token associated with branch ${branch}.`,
      must_contain: [orch],
      kind: "branch_to_orch",
      orch,
      branch,
    });
  }
  return expected.slice(0, 50);
}

async function main() {
  const mem0PkgRoot =
    process.env.MEM0_PKG_ROOT ||
    path.join(
      os.homedir(),
      ".smartclaw",
      "extensions",
      "openclaw-mem0",
      "node_modules",
      "mem0ai",
    );
  const mem0OssModule = path.join(mem0PkgRoot, "dist", "oss", "index.mjs");
  if (!fs.existsSync(mem0OssModule)) {
    throw new Error(`mem0 oss module not found: ${mem0OssModule}`);
  }

  const outRoot = process.env.MEM0_OFFLINE_OUT || "/tmp/openclaw-mem0-offline";
  const runDir = path.join(outRoot, `${utcStamp()}-offline-50q`);
  fs.mkdirSync(runDir, { recursive: true });
  const latest = path.join(outRoot, "latest");
  try {
    fs.unlinkSync(latest);
  } catch { }
  fs.symlinkSync(runDir, latest);

  const slackDir = path.join(os.homedir(), ".smartclaw", "memory", "slack-history");
  const files = listMarkdownFiles(slackDir);
  const allText = files.map((f) => fs.readFileSync(f, "utf8")).join("\n");
  const pairs = buildPairMap(allText);
  const expected = buildExpected(pairs);
  fs.writeFileSync(
    path.join(runDir, "expected-50.json"),
    JSON.stringify(expected, null, 2),
  );

  const { Memory } = await import(`file://${mem0OssModule}`);

  const embedderProvider = process.env.MEM0_EMBED_PROVIDER || "openai";
  const embedderModel = process.env.MEM0_EMBED_MODEL || "text-embedding-3-small";
  const openaiApiKey = process.env.OPENAI_API_KEY || "";
  if (embedderProvider === "openai" && !openaiApiKey) {
    throw new Error("OPENAI_API_KEY is required for MEM0_EMBED_PROVIDER=openai");
  }

  const vectorProvider = process.env.MEM0_VECTOR_PROVIDER || "memory";
  const vectorStore =
    vectorProvider === "qdrant"
      ? {
        provider: "qdrant",
        config: {
          host: process.env.MEM0_QDRANT_HOST || "127.0.0.1",
          port: Number(process.env.MEM0_QDRANT_PORT || "6333"),
          collectionName: process.env.MEM0_QDRANT_COLLECTION || "openclaw_mem0",
          embeddingModelDims: Number(process.env.MEM0_QDRANT_DIMS || "1536"),
          checkCompatibility:
            String(process.env.MEM0_QDRANT_CHECK_COMPAT || "false") !== "false",
        },
      }
      : { provider: "memory", config: {} };

  const memory = new Memory({
    version: "v1.1",
    embedder: {
      provider: embedderProvider,
      config:
        embedderProvider === "openai"
          ? { apiKey: openaiApiKey, model: embedderModel }
          : { model: embedderModel },
    },
    vectorStore,
  });

  const userId = process.env.MEM0_TEST_USER_ID || `offline-50q-${Date.now()}`;
  const source = process.env.MEM0_TEST_SOURCE || "offline-50q";

  const canonicalLines = [];
  for (const [orch, branch] of pairs.entries()) {
    canonicalLines.push(`${orch} committed to ${branch}`);
    canonicalLines.push(`${branch} maps to ${orch}`);
    canonicalLines.push(`Which branch was ${orch} committed to? ${branch}`);
    canonicalLines.push(`Find the ORCH token associated with branch ${branch}. ${orch}`);
  }

  let ingested = 0;
  for (const line of canonicalLines) {
    await memory.add([{ role: "user", content: line }], {
      userId,
      source,
      infer: false,
    });
    ingested += 1;
  }

  const rows = [];
  let passed = 0;
  for (let i = 0; i < expected.length; i += 1) {
    const item = expected[i];
    const query =
      item.kind === "orch_to_branch"
        ? `${item.orch} committed to ai-orch`
        : `${item.branch} maps to ORCH`;
    const query2 =
      item.kind === "orch_to_branch"
        ? `${item.orch} committed to ${item.branch}`
        : `${item.branch} maps to ${item.orch}`;

    const collect = async (q) => {
      const raw = await memory.search(q, {
        userId,
        source,
        limit: 20,
        keyword_search: true,
      });
      return Array.isArray(raw) ? raw : Array.isArray(raw?.results) ? raw.results : [];
    };

    const results1 = await collect(query);
    const results2 = await collect(query2);
    const seen = new Set();
    const results = [];
    for (const r of [...results1, ...results2]) {
      const id = String(r?.id || "") + "|" + String(r?.memory || "");
      if (seen.has(id)) continue;
      seen.add(id);
      results.push(r);
    }
    const haystack = results
      .map((r) => `${r?.memory || ""} ${JSON.stringify(r || {})}`)
      .join("\n")
      .toLowerCase();
    const ok = item.must_contain.every((m) => haystack.includes(m.toLowerCase()));
    if (ok) passed += 1;
    rows.push({
      n: i + 1,
      question: item.question,
      query,
      query_exact: query2,
      must_contain: item.must_contain,
      passed: ok,
      top_result: results[0]?.memory || "",
      result_count: results.length,
    });
  }

  const score = {
    ingested,
    pair_count: pairs.size,
    passed,
    total: expected.length,
    pass_rate: expected.length ? passed / expected.length : 0,
    userId,
    source,
    runDir,
  };
  fs.writeFileSync(path.join(runDir, "qa-50.json"), JSON.stringify(rows, null, 2));
  fs.writeFileSync(path.join(runDir, "score.json"), JSON.stringify(score, null, 2));
  fs.writeFileSync(
    path.join(runDir, "failures.json"),
    JSON.stringify(rows.filter((r) => !r.passed), null, 2),
  );

  console.log(JSON.stringify(score, null, 2));
}

main().catch((err) => {
  console.error(String(err?.stack || err));
  process.exit(1);
});
