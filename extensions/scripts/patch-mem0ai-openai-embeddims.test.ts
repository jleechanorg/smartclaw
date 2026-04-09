/**
 * TDD: mem0ai OpenAIEmbedder must pass dimensions to OpenAI embeddings.create
 * when embeddingDims is set (matches Qdrant collection size).
 */
import { describe, it, expect } from "vitest";
import { patchMem0aiOpenAiEmbedCalls } from "./patch-mem0ai-openai-embeddims.mjs";

describe("patchMem0aiOpenAiEmbedCalls", () => {
  const unpatched = `
var OpenAIEmbedder = class {
  async embed(text) {
    const response = await this.openai.embeddings.create({
      model: this.model,
      input: text
    });
    return response.data[0].embedding;
  }
  async embedBatch(texts) {
    const response = await this.openai.embeddings.create({
      model: this.model,
      input: texts
    });
    return response.data.map((item) => item.embedding);
  }
};
`;

  it("adds dimensions: this.embeddingDims to embed and embedBatch", () => {
    const out = patchMem0aiOpenAiEmbedCalls(unpatched);
    expect(out).toContain("dimensions: this.embeddingDims");
    expect(out.match(/dimensions: this.embeddingDims/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("is idempotent", () => {
    const once = patchMem0aiOpenAiEmbedCalls(unpatched);
    const twice = patchMem0aiOpenAiEmbedCalls(once);
    expect(twice).toBe(once);
  });
});
