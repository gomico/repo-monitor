const GITHUB_DISPATCH_URL =
  "https://api.github.com/repos/gomico/repo-monitor/actions/workflows/nightly.yml/dispatches";
const GITHUB_API_VERSION = "2026-03-10";
const USER_AGENT = "gomico-repo-monitor-cloudflare-scheduler";

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=UTF-8" },
  });
}

function constantTimeEqual(expected, actual) {
  if (!expected || !actual || expected.length !== actual.length) return false;

  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ actual.charCodeAt(index);
  }
  return difference === 0;
}

function bearerToken(request) {
  const authorization = request.headers.get("authorization") || "";
  const prefix = "Bearer ";
  return authorization.startsWith(prefix) ? authorization.slice(prefix.length) : "";
}

async function dispatchNightlyReport(env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN is not configured");
  }

  let response;
  try {
    response = await fetch(GITHUB_DISPATCH_URL, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { no_publish: "false" },
      }),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    console.error("GitHub workflow dispatch request failed before receiving a response", {
      status: null,
      response: detail,
    });
    throw error;
  }

  const responseBody = await response.text();
  if (!response.ok) {
    console.error("GitHub workflow dispatch failed", {
      status: response.status,
      response: responseBody,
    });
    throw new Error(
      `GitHub workflow dispatch failed: HTTP ${response.status}: ${responseBody}`,
    );
  }

  console.log("GitHub workflow dispatch accepted", {
    status: response.status,
    response: responseBody,
  });
  return response.status;
}

export default {
  async scheduled(controller, env, ctx) {
    void ctx;
    console.log("Cloudflare Cron fired", {
      cron: controller.cron,
      scheduledTime: new Date(controller.scheduledTime).toISOString(),
    });
    await dispatchNightlyReport(env);
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return jsonResponse({ ok: true, service: "repo-monitor-nightly-scheduler" });
    }

    if (url.pathname !== "/trigger") {
      return new Response("Not found", { status: 404 });
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { Allow: "POST" },
      });
    }

    if (!env.TRIGGER_SECRET) {
      return jsonResponse({ ok: false, error: "TRIGGER_SECRET is not configured" }, 503);
    }

    if (!constantTimeEqual(env.TRIGGER_SECRET, bearerToken(request))) {
      return jsonResponse({ ok: false, error: "Unauthorized" }, 401);
    }

    const status = await dispatchNightlyReport(env);
    return jsonResponse({ ok: true, githubStatus: status });
  },
};
