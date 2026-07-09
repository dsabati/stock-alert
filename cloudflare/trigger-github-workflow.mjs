export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerGitHubWorkflow(env));
  },
};

async function triggerGitHubWorkflow(env) {
  const owner = env.GITHUB_OWNER;
  const repo = env.GITHUB_REPO;
  const workflowFile = env.GITHUB_WORKFLOW_FILE || "check.yml";
  const ref = env.GITHUB_REF || "main";

  if (!owner || !repo) {
    throw new Error("Missing required vars: GITHUB_OWNER and GITHUB_REPO");
  }

  if (!env.GITHUB_TOKEN) {
    throw new Error("Missing required secret: GITHUB_TOKEN");
  }

  const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`;
  const response = await fetch(dispatchUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "cloudflare-worker-github-dispatch",
    },
    body: JSON.stringify({ ref }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `GitHub workflow dispatch failed (${response.status}): ${body}`
    );
  }

  console.log(`Dispatched ${workflowFile} on ${owner}/${repo}@${ref}`);
}
