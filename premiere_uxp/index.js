"use strict";

const ppro = require("premierepro");
const { entrypoints, host } = require("uxp");

const PLUGIN_VERSION = "1.0.0";
const BASE_URLS = [
  "http://127.0.0.1:43127",
  "http://127.0.0.1:43128",
  "http://127.0.0.1:43129",
];
const CONNECTED_POLL_MS = 1000;
const DISCONNECTED_POLL_MS = 2000;
const LEASE_RENEW_MS = 30000;

let running = false;
let pollTimer = null;
let activeBaseUrl = null;
let activeLeaseStop = null;
const authTokens = new Map();

function requestJson(method, url, payload, authToken) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(method, url, true);
    request.timeout = 5000;
    request.setRequestHeader("Accept", "application/json");
    if (payload !== undefined) {
      request.setRequestHeader("Content-Type", "text/plain;charset=UTF-8");
    }
    if (authToken) {
      request.setRequestHeader("X-Clip-Extractor-Token", authToken);
    }
    request.onload = () => {
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(`Bridge HTTP ${request.status}`));
        return;
      }
      try {
        resolve(request.responseText ? JSON.parse(request.responseText) : {});
      } catch (error) {
        reject(new Error(`Bridge returned invalid JSON: ${error.message}`));
      }
    };
    request.onerror = () => reject(new Error("Bridge connection failed"));
    request.ontimeout = () => reject(new Error("Bridge connection timed out"));
    request.send(payload === undefined ? null : JSON.stringify(payload));
  });
}

function normalizedPath(value) {
  const original = String(value || "");
  const withSlashes = original.replace(/\\/g, "/").replace(/\/+$/, "");
  const isWindowsPath =
    original.includes("\\") || /^[A-Za-z]:\//.test(withSlashes);
  return isWindowsPath ? withSlashes.toLowerCase() : withSlashes;
}

async function mediaItemsByPath(project) {
  const rootItem = await project.getRootItem();
  const items = [...(await rootItem.getItems())];
  const found = new Map();

  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const clip = ppro.ClipProjectItem.cast(item);
    if (clip) {
      const contentType = await clip.getContentType();
      if (contentType === ppro.Constants.ContentType.MEDIA) {
        const mediaPath = await clip.getMediaFilePath();
        if (mediaPath) {
          found.set(normalizedPath(mediaPath), clip);
        }
      }
      continue;
    }

    const folder = ppro.FolderItem.cast(item);
    if (folder) {
      items.push(...(await folder.getItems()));
    }
  }
  return found;
}

async function obtainProject(job) {
  const activeProject = await ppro.Project.getActiveProject();
  if (activeProject) {
    return { project: activeProject, created: false };
  }

  const project = await ppro.Project.createProject(job.project_path);
  if (!project) {
    throw new Error("Premiere project could not be created");
  }
  return { project, created: true };
}

async function importJob(job) {
  if (!job || job.action !== "import_clips") {
    throw new Error("Unsupported Clip Extractor job");
  }
  if (!Array.isArray(job.media) || job.media.length === 0) {
    throw new Error("The job contains no media");
  }

  const { project, created } = await obtainProject(job);
  let mediaByPath = await mediaItemsByPath(project);
  const missingPaths = [];

  for (const media of job.media) {
    const key = normalizedPath(media.path);
    if (!mediaByPath.has(key) && !missingPaths.includes(media.path)) {
      missingPaths.push(media.path);
    }
  }

  if (missingPaths.length > 0) {
    const imported = await project.importFiles(
      missingPaths,
      true,
      undefined,
      false
    );
    if (!imported) {
      throw new Error("Premiere rejected one or more media files");
    }
    mediaByPath = await mediaItemsByPath(project);
  }

  const existingSequences = await project.getSequences();
  const sequencesByName = new Map(
    existingSequences.map((sequence) => [String(sequence.name), sequence])
  );
  let firstSequence = null;
  let createdSequenceCount = 0;

  for (const media of job.media) {
    const clip = mediaByPath.get(normalizedPath(media.path));
    if (!clip) {
      throw new Error(`Imported media was not found: ${media.path}`);
    }

    const sequenceName = String(media.sequence_name || "Clip Extractor");
    let sequence = sequencesByName.get(sequenceName);
    if (!sequence) {
      sequence = await project.createSequenceFromMedia(sequenceName, [clip]);
      if (!sequence) {
        throw new Error(`Sequence could not be created: ${sequenceName}`);
      }
      sequencesByName.set(sequenceName, sequence);
      createdSequenceCount += 1;
    }
    if (!firstSequence) {
      firstSequence = sequence;
    }
  }

  if (job.open_first_sequence && firstSequence) {
    const opened = await project.openSequence(firstSequence);
    if (!opened) {
      throw new Error("Premiere could not open the first sequence");
    }
  }
  if (created) {
    const saved = await project.save();
    if (!saved) {
      throw new Error("Premiere project could not be saved");
    }
  }

  return {
    success: true,
    message: `${job.media.length}件の素材を読み込み、${createdSequenceCount}件のシーケンスを作成しました`,
    imported_count: missingPaths.length,
    sequence_count: createdSequenceCount,
    created_project: created,
  };
}

async function reportResult(baseUrl, authToken, job, result) {
  await requestJson(
    "POST",
    `${baseUrl}/v1/jobs/${job.id}/result`,
    {
      ...result,
      lease_token: job.lease_token,
    },
    authToken
  );
}

function startLeaseRenewal(baseUrl, authToken, job) {
  let stopped = false;
  let timer = null;

  const renew = async () => {
    if (stopped) {
      return;
    }
    try {
      await requestJson(
        "POST",
        `${baseUrl}/v1/jobs/${job.id}/renew`,
        { lease_token: job.lease_token },
        authToken
      );
    } catch (error) {
      console.error("[Clip Extractor] Failed to renew job lease:", error);
    }
    if (!stopped) {
      timer = setTimeout(renew, LEASE_RENEW_MS);
    }
  };

  timer = setTimeout(renew, LEASE_RENEW_MS);
  return () => {
    stopped = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
}

async function processJob(baseUrl, authToken, job) {
  const stopLeaseRenewal = startLeaseRenewal(baseUrl, authToken, job);
  activeLeaseStop = stopLeaseRenewal;
  let result;
  try {
    result = await importJob(job);
  } catch (error) {
    const message =
      error && error.message ? String(error.message) : String(error);
    result = {
      success: false,
      message,
    };
  }
  stopLeaseRenewal();
  if (activeLeaseStop === stopLeaseRenewal) {
    activeLeaseStop = null;
  }

  try {
    await reportResult(baseUrl, authToken, job, result);
  } catch (reportError) {
    console.error(
      "[Clip Extractor] Failed to report job result:",
      reportError
    );
  }
}

async function pollBridge() {
  const orderedUrls = activeBaseUrl
    ? [
        activeBaseUrl,
        ...BASE_URLS.filter((baseUrl) => baseUrl !== activeBaseUrl),
      ]
    : BASE_URLS;

  for (const baseUrl of orderedUrls) {
    try {
      let authToken = authTokens.get(baseUrl);
      if (!authToken) {
        const session = await requestJson(
          "GET",
          `${baseUrl}/v1/session`
        );
        authToken = String(session.token || "");
        if (!authToken) {
          throw new Error("Bridge did not issue a session token");
        }
        authTokens.set(baseUrl, authToken);
      }
      await requestJson("POST", `${baseUrl}/v1/heartbeat`, {
        plugin_version: PLUGIN_VERSION,
        premiere_version: String(host.version || ""),
      }, authToken);
      activeBaseUrl = baseUrl;
      const response = await requestJson(
        "POST",
        `${baseUrl}/v1/jobs/next`,
        {},
        authToken
      );
      if (response.job) {
        await processJob(baseUrl, authToken, response.job);
      }
      return CONNECTED_POLL_MS;
    } catch (error) {
      authTokens.delete(baseUrl);
      if (baseUrl === activeBaseUrl) {
        activeBaseUrl = null;
      }
    }
  }
  return DISCONNECTED_POLL_MS;
}

async function pollLoop() {
  if (!running) {
    return;
  }
  let delay = DISCONNECTED_POLL_MS;
  try {
    delay = await pollBridge();
  } catch (error) {
    console.error("[Clip Extractor] Bridge polling failed:", error);
  }
  if (running) {
    pollTimer = setTimeout(pollLoop, delay);
  }
}

function startPolling() {
  if (running) {
    return;
  }
  running = true;
  pollLoop();
}

function stopPolling() {
  running = false;
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (activeLeaseStop) {
    activeLeaseStop();
    activeLeaseStop = null;
  }
}

entrypoints.setup({
  plugin: {
    create() {
      startPolling();
    },
    destroy() {
      stopPolling();
    },
  },
  commands: {
    bridge() {
      startPolling();
    },
  },
});
