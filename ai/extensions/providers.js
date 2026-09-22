// Register the two model providers used by the StockDesk AI agent.
// Keys are read from env vars KIMI_API_KEY / OPENCODE_API_KEY.
export default function (pi) {
  pi.registerProvider("kimi", {
    name: "Kimi (api.kimi.com)",
    // 走本机 key 故障转移代理（kimi-failover-proxy.js，127.0.0.1:8799）：
    // 主 key 额度耗尽自动改用 KIMI_API_KEY_BACKUP 重发，主 key 恢复后自动切回。
    baseUrl: process.env.KIMI_BASE_URL || "http://127.0.0.1:8799/v1",
    apiKey: process.env.KIMI_API_KEY,
    authHeader: true,
    api: "openai-completions",
    models: [
      {
        id: "k3",
        name: "Kimi k3",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 256000,
        maxTokens: 16384,
      },
    ],
  });

  pi.registerProvider("oczen", {
    name: "OpenCode Zen",
    baseUrl: "https://opencode.ai/zen/go/v1",
    apiKey: process.env.OPENCODE_API_KEY,
    authHeader: true,
    api: "openai-completions",
    models: [
      {
        id: "deepseek-v4-flash",
        name: "DeepSeek v4 Flash",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
      {
        id: "deepseek-v4-flash-vision-exp",
        name: "DeepSeek v4 Flash Vision",
        reasoning: false,
        input: ["text", "image"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
    ],
  });

  // 专家身份注入：worker 在用户选择 skill 时设置 SKILL_IDENTITY，
  // 每轮请求前把它追加进系统提示词，让 agent 以该领域专家身份工作。
  pi.on("before_agent_start", async (event) => {
    const identity = process.env.SKILL_IDENTITY;
    if (!identity) return undefined;
    return { systemPrompt: event.systemPrompt + "\n\n" + identity };
  });
}
