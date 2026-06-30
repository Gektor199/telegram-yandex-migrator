const form = document.getElementById("import-form");
const submitButton = document.getElementById("submit-button");
const toastRoot = document.getElementById("toast-root");
const jobsList = document.getElementById("jobs-list");
const jobsEmpty = document.getElementById("jobs-empty");
const targetLabel = document.getElementById("target-label");
const targetCopy = document.getElementById("target-copy");
const targetInput = document.getElementById("target-input");
const archiveInput = document.querySelector('#import-form input[name="archive"]');
const additionalMembersInput = document.getElementById("additional-members-input");
const uploadProgress = document.getElementById("archive-upload-progress");
const uploadProgressBar = document.getElementById("archive-upload-bar");
const uploadProgressCopy = document.getElementById("archive-upload-copy");
const uploadProgressTitle = document.getElementById("archive-upload-title");
const uploadActions = document.getElementById("archive-upload-actions");
const resumeUploadButton = document.getElementById("resume-upload-button");
const resetUploadButton = document.getElementById("reset-upload-button");
const targetKindInputs = document.querySelectorAll('input[name="target_kind"]');
const csrfToken = document.querySelector('#import-form input[name="csrf_token"]')?.value || "";
let activeUploadSession = window.__ACTIVE_UPLOAD_SESSION__ || null;

const jobs = new Map((window.__INITIAL_JOBS__ || []).map((job) => [job.id, job]));
const openJobs = new Set();
let pollTimer = null;
let uploadInProgress = false;

targetKindInputs.forEach((input) => {
  input.addEventListener("change", syncTargetKindUi);
});
syncTargetKindUi();
restoreUploadProgress();
renderJobs();
startPolling();

if (form && submitButton) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const archiveFile = archiveInput?.files?.[0];
    const targetValue = (targetInput?.value || "").trim();
    if (!archiveFile || !targetValue) {
      showToast("Заполните архив и название нового чата.", "error");
      return;
    }
    if (activeUploadSession) {
      showToast("Есть незавершённая загрузка архива. Нажмите «Продолжить» или «Сбросить загрузку».", "error");
      return;
    }

    let uploadId = "";
    uploadInProgress = true;
    setSubmitting(true, "Подготавливаю загрузку...");
    try {
      const session = await createUploadSession(archiveFile);
      uploadId = session.upload_id;
      activeUploadSession = {
        id: session.upload_id,
        archive_name: archiveFile.name,
        state: "uploading",
        total_bytes: archiveFile.size,
        received_bytes: 0,
      };
      showUploadProgress(0, "Готовлю отправку архива...");
      setSubmitting(true, "Загружаю архив...");
      await uploadArchiveInChunks(uploadId, archiveFile, session.chunk_size);
      setSubmitting(true, "Создаю задачу...");
      const data = await completeUploadSession(uploadId);
      showToast("Задача поставлена в очередь.", "success");
      form.reset();
      syncTargetKindUi();
      resetUploadProgress();
      const statusResponse = await fetch(`/api/imports/${data.job_id}`);
      const job = await parseJson(statusResponse);
      if (statusResponse.ok) {
        jobs.set(job.id, job);
        renderJobs();
      }
    } catch (error) {
      if (uploadId) {
        await dropUploadSession(uploadId);
      }
      showToast(error.message, "error");
    } finally {
      uploadInProgress = false;
      setSubmitting(false, "Создать чат и поставить импорт в очередь");
    }
  });
}

window.addEventListener("beforeunload", (event) => {
  if (!uploadInProgress) {
    return;
  }
  event.preventDefault();
  event.returnValue = "";
});

if (resumeUploadButton) {
  resumeUploadButton.addEventListener("click", async () => {
    if (!activeUploadSession) {
      return;
    }
    const archiveFile = archiveInput?.files?.[0];
    const targetValue = (targetInput?.value || "").trim();
    if (!archiveFile) {
      showToast("Выберите тот же архив, чтобы продолжить загрузку.", "error");
      return;
    }
    if (!targetValue) {
      showToast("Укажите название нового чата.", "error");
      return;
    }
    if (
      archiveFile.name !== activeUploadSession.archive_name ||
      Number(archiveFile.size || 0) !== Number(activeUploadSession.total_bytes || 0)
    ) {
      showToast("Для продолжения выберите тот же архив, который загружался ранее.", "error");
      return;
    }

    uploadInProgress = true;
    setSubmitting(true, "Продолжаю загрузку...");
    try {
      const chunkSize = 32 * 1024 * 1024;
      const receivedBytes = Number(activeUploadSession.received_bytes || 0);
      if (receivedBytes < Number(activeUploadSession.total_bytes || 0)) {
        await uploadArchiveInChunks(activeUploadSession.id, archiveFile, chunkSize, receivedBytes);
      }
      setSubmitting(true, "Создаю задачу...");
      const data = await completeUploadSession(activeUploadSession.id);
      showToast("Задача поставлена в очередь.", "success");
      form.reset();
      syncTargetKindUi();
      resetUploadProgress();
      const statusResponse = await fetch(`/api/imports/${data.job_id}`);
      const job = await parseJson(statusResponse);
      if (statusResponse.ok) {
        jobs.set(job.id, job);
        renderJobs();
      }
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      uploadInProgress = false;
      setSubmitting(false, "Создать чат и поставить импорт в очередь");
    }
  });
}

if (resetUploadButton) {
  resetUploadButton.addEventListener("click", async () => {
    if (!activeUploadSession) {
      return;
    }
    try {
      await dropUploadSession(activeUploadSession.id);
      resetUploadProgress();
      showToast("Незавершённая загрузка сброшена.", "success");
    } catch (error) {
      showToast(error.message || "Не удалось сбросить загрузку.", "error");
    }
  });
}

function setSubmitting(isSubmitting, label = "Создать чат и поставить импорт в очередь") {
  if (!submitButton) return;
  submitButton.disabled = isSubmitting;
  submitButton.textContent = label;
}

function startPolling() {
  stopPolling();
  pollTimer = window.setInterval(pollJobs, 1500);
  pollJobs();
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJobs() {
  const activeIds = [...jobs.values()]
    .filter((job) => job.state === "queued" || job.state === "running" || job.state === "pause_requested")
    .map((job) => job.id);

  await Promise.all(
    activeIds.map(async (jobId) => {
      try {
        const response = await fetch(`/api/imports/${jobId}`);
        const data = await parseJson(response);
        if (!response.ok) {
          throw new Error(extractErrorMessage(data, `Не удалось обновить задачу ${jobId}`));
        }
        const previous = jobs.get(jobId);
        jobs.set(jobId, data);
        if (data.error_count > (previous?.error_count || 0) && data.last_error) {
          showToast(data.last_error, "error");
        }
        if (previous && previous.state !== data.state && data.state === "completed") {
          showToast(`Импорт ${data.archive_name} завершен.`, data.error_count > 0 ? "error" : "success");
        }
        if (previous && previous.state !== data.state && data.state === "failed") {
          showToast(data.detail || `Импорт ${data.archive_name} завершился ошибкой.`, "error");
        }
        if (previous && previous.state !== data.state && data.state === "paused") {
          showToast(`Импорт ${data.archive_name} остановлен.`, "success");
        }
      } catch (error) {
        showToast(error.message, "error");
      }
    }),
  );

  renderJobs();
}

function renderJobs() {
  const orderedJobs = [...jobs.values()].sort((left, right) => {
    const leftTime = Date.parse(left.created_at || 0);
    const rightTime = Date.parse(right.created_at || 0);
    return rightTime - leftTime;
  });

  jobsList.replaceChildren();
  jobsEmpty.classList.toggle("hidden", orderedJobs.length > 0);

  for (const job of orderedJobs) {
    const row = buildJobRow(job);
    row.addEventListener("toggle", () => {
      if (row.open) {
        openJobs.add(job.id);
      } else {
        openJobs.delete(job.id);
      }
    });
    jobsList.appendChild(row);
  }
}

function buildJobRow(job) {
  const row = createNode("details", "job-row");
  row.dataset.jobId = job.id;
  row.open = openJobs.has(job.id);

  const summary = createNode("summary", "job-row-summary");

  const primary = createNode("div", "job-row-primary");
  primary.appendChild(createTextNode("p", "caption", job.target_kind === "channel" ? "Новый канал" : "Новый чат"));
  primary.appendChild(createTextNode("h3", "", job.archive_name));
  primary.appendChild(createTextNode("p", "job-target", job.target_value || ""));

  const progress = createNode("div", "job-row-progress");
  progress.appendChild(createTextNode("p", "asset-progress-label", job.progress_label || "Отправка сообщений"));
  const track = createNode("div", "progress-track");
  const bar = createNode("div", `progress-bar ${job.state === "running" ? "is-running" : ""}`.trim());
  bar.style.width = `${job.progress}%`;
  track.appendChild(bar);
  progress.appendChild(track);

  const metrics = createNode("div", "job-row-metrics");
  metrics.appendChild(createTextNode("span", "", `${job.progress}%`));
  metrics.appendChild(createTextNode("span", "", `${job.progress_current} / ${job.progress_total}`));
  metrics.appendChild(createTextNode("span", "", `Ошибки: ${job.error_count}`));

  const state = createNode("div", "job-row-state");
  state.appendChild(createTextNode("span", `badge ${badgeClass(job.state)}`, stateLabel(job.state)));

  summary.append(primary, progress, metrics, state);

  const details = createNode("div", "job-row-details");
  const meta = createNode("div", "job-row-meta");
  meta.appendChild(createTextNode("p", "status-detail", job.detail || ""));
  if (job.resolved_chat_id) {
    meta.appendChild(createTextNode("p", "status-meta", `chat_id: ${job.resolved_chat_id}`));
  }
  meta.appendChild(createTextNode("p", "status-current", job.current_item || "Ожидание следующего сообщения"));
  if (job.asset_total > 0) {
    meta.appendChild(
      createTextNode(
        "p",
        "status-current asset-status-copy",
        `Файлы на Диске: ${job.asset_processed}/${job.asset_total} · готово ${job.asset_ready} · повторить ${job.asset_retryable}`,
      ),
    );
  }
  meta.appendChild(buildJobActions(job));

  const events = createNode("div", "job-row-events");
  events.appendChild(buildEventBlock("Действия", (Array.isArray(job.events) ? job.events : []).filter((entry) => entry.level !== "error"), "Действий пока нет."));
  events.appendChild(buildEventBlock("Ошибки", (Array.isArray(job.events) ? job.events : []).filter((entry) => entry.level === "error"), "Ошибок нет."));

  details.append(meta, events);
  row.append(summary, details);
  return row;
}

function buildEventBlock(title, entries, emptyMessage) {
  const block = createNode("div", "job-row-events-block");
  block.appendChild(createTextNode("p", "caption", title));
  const list = createNode("ul", "log-list compact");

  if (!entries.length) {
    list.appendChild(createTextNode("li", "log-list-empty", emptyMessage));
  } else {
    for (const entry of entries) {
      list.appendChild(createTextNode("li", "", entry.message || ""));
    }
  }

  block.appendChild(list);
  return block;
}

function buildJobActions(job) {
  const wrap = createNode("div", "job-actions");

  if (job.can_pause) {
    const pauseButton = createNode("button", "secondary-link job-action-button");
    pauseButton.type = "button";
    pauseButton.textContent = job.state === "pause_requested" ? "Останавливаю..." : "Остановить";
    pauseButton.disabled = job.state === "pause_requested";
    pauseButton.addEventListener("click", () => submitJobAction(job.id, "pause", pauseButton));
    wrap.appendChild(pauseButton);
  }

  if (job.can_resume) {
    const resumeButton = createNode("button", "secondary-link job-action-button");
    resumeButton.type = "button";
    resumeButton.textContent = "Продолжить";
    resumeButton.addEventListener("click", () => submitJobAction(job.id, "resume", resumeButton));
    wrap.appendChild(resumeButton);
  }

  if (job.can_retry) {
    const retryButton = createNode("button", "secondary-link job-action-button");
    retryButton.type = "button";
    retryButton.textContent = "Retry";
    retryButton.addEventListener("click", () => submitJobAction(job.id, "retry", retryButton));
    wrap.appendChild(retryButton);
  }

  return wrap;
}

async function submitJobAction(jobId, action, button) {
  if (!csrfToken) {
    showToast("Не найден CSRF token.", "error");
    return;
  }

  if (button) {
    button.disabled = true;
  }

  try {
    const payload = new FormData();
    payload.set("csrf_token", csrfToken);
    const response = await fetch(`/api/imports/${jobId}/${action}`, {
      method: "POST",
      body: payload,
    });
    const data = await parseJson(response);
    if (!response.ok) {
      throw new Error(extractErrorMessage(data, "Не удалось изменить состояние задачи."));
    }
    jobs.set(data.id, data);
    renderJobs();
    const successMessage =
      action === "pause"
        ? "Команда на остановку отправлена."
        : action === "resume"
          ? "Задача поставлена на продолжение."
          : "Повторная попытка отправки ссылок поставлена в очередь.";
    showToast(successMessage, "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

function syncTargetKindUi() {
  const selected = document.querySelector('input[name="target_kind"]:checked')?.value || "chat";
  if (selected === "channel") {
    targetLabel.textContent = "Наименование нового канала";
    targetCopy.textContent = "Будет создан новый канал. Бот создаст его автоматически и назначит вас администратором.";
    targetInput.placeholder = "Например: Новости продукта";
    return;
  }

  targetLabel.textContent = "Наименование нового чата";
  targetCopy.textContent = "Будет создан новый групповой чат. Бот автоматически назначит вас администратором.";
  targetInput.placeholder = "Например: Архив проектной группы";
}

async function createUploadSession(file) {
  const response = await fetch("/api/uploads", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({
      archive_name: file.name,
      total_bytes: file.size,
    }),
  });
  const data = await parseJson(response);
  if (!response.ok) {
    throw new Error(extractErrorMessage(data, "Не удалось создать сессию загрузки архива."));
  }
  return data;
}

async function uploadArchiveInChunks(uploadId, file, chunkSize, startingOffset = 0) {
  const totalBytes = file.size;
  let offset = startingOffset;
  let chunkIndex = Math.floor(offset / chunkSize);

  while (offset < totalBytes) {
    const chunk = file.slice(offset, offset + chunkSize);
    const response = await fetch(`/api/uploads/${uploadId}/chunks?chunk_index=${chunkIndex}&offset=${offset}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-CSRF-Token": csrfToken,
      },
      body: chunk,
    });
    const data = await parseJson(response);
    if (!response.ok) {
      throw new Error(extractErrorMessage(data, "Не удалось передать очередной чанк архива."));
    }
    offset = Number(data.received_bytes || offset + chunk.size);
    if (activeUploadSession && activeUploadSession.id === uploadId) {
      activeUploadSession = { ...activeUploadSession, received_bytes: offset, state: offset >= totalBytes ? "uploaded" : "uploading" };
    }
    chunkIndex += 1;
    showUploadProgress(
      Math.min(100, Math.round((offset / totalBytes) * 1000) / 10),
      `Загрузка архива: ${formatBytes(offset)} из ${formatBytes(totalBytes)}`,
    );
  }
}

async function completeUploadSession(uploadId) {
  const response = await fetch(`/api/uploads/${uploadId}/complete`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({
      target_kind: document.querySelector('input[name="target_kind"]:checked')?.value || "chat",
      target: (targetInput?.value || "").trim(),
      additional_members: additionalMembersInput?.value || "",
    }),
  });
  const data = await parseJson(response);
  if (!response.ok) {
    throw new Error(extractErrorMessage(data, "Не удалось поставить импорт в очередь."));
  }
  return data;
}

async function dropUploadSession(uploadId) {
  try {
    await fetch(`/api/uploads/${uploadId}`, {
      method: "DELETE",
      headers: { "X-CSRF-Token": csrfToken },
    });
  } catch {
    // Best-effort cleanup only.
  }
}

function showUploadProgress(percent, copy) {
  if (!uploadProgress || !uploadProgressBar || !uploadProgressCopy) {
    return;
  }
  uploadProgress.classList.remove("hidden");
  uploadProgressBar.style.width = `${percent}%`;
  uploadProgressCopy.textContent = `${percent}%`;
  if (uploadProgressTitle) {
    uploadProgressTitle.textContent = copy;
  }
  if (uploadActions) {
    uploadActions.classList.toggle("hidden", !activeUploadSession);
  }
}

function restoreUploadProgress() {
  if (!activeUploadSession || !uploadProgress) {
    return;
  }
  const totalBytes = Number(activeUploadSession.total_bytes || 0);
  const receivedBytes = Number(activeUploadSession.received_bytes || 0);
  if (activeUploadSession.state !== "uploading" && activeUploadSession.state !== "uploaded") {
    return;
  }
  const percent = totalBytes > 0 ? Math.min(100, Math.round((receivedBytes / totalBytes) * 1000) / 10) : 0;
  uploadInProgress = activeUploadSession.state === "uploading";
  showUploadProgress(
    percent,
    `Загрузка архива: ${formatBytes(receivedBytes)} из ${formatBytes(totalBytes)}`,
  );
}

function resetUploadProgress() {
  if (!uploadProgress || !uploadProgressBar || !uploadProgressCopy) {
    return;
  }
  uploadProgress.classList.add("hidden");
  uploadProgressBar.style.width = "0%";
  uploadProgressCopy.textContent = "0%";
  if (uploadProgressTitle) {
    uploadProgressTitle.textContent = "Загрузка архива";
  }
  if (uploadActions) {
    uploadActions.classList.add("hidden");
  }
  activeUploadSession = null;
  uploadInProgress = false;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const fractionDigits = unitIndex === 0 ? 0 : unitIndex === 1 ? 0 : 1;
  return `${value.toFixed(fractionDigits)} ${units[unitIndex]}`;
}

function badgeClass(state) {
  if (state === "running") return "badge-running";
  if (state === "pause_requested") return "badge-running";
  if (state === "completed") return "badge-completed";
  if (state === "failed") return "badge-failed";
  return "badge-soft";
}

function stateLabel(state) {
  if (state === "queued") return "QUEUED";
  if (state === "running") return "RUNNING";
  if (state === "pause_requested") return "PAUSING";
  if (state === "paused") return "PAUSED";
  if (state === "completed") return "COMPLETED";
  if (state === "failed") return "FAILED";
  return state;
}

function showToast(message, tone = "success") {
  const toast = createNode("div", `toast ${tone}`);
  toast.appendChild(createTextNode("p", "toast-title", tone === "error" ? "Ошибка" : "Статус"));
  toast.appendChild(createTextNode("p", "toast-message", message));
  toastRoot.appendChild(toast);
  window.setTimeout(() => toast.remove(), 5000);
}

async function parseJson(response) {
  try {
    return await response.json();
  } catch {
    try {
      const text = await response.text();
      return { detail: text.trim() };
    } catch {
      return {};
    }
  }
}

function extractErrorMessage(data, fallback) {
  if (!data || typeof data !== "object") {
    return fallback;
  }

  const detail = normalizeErrorValue(data.detail);
  if (detail) {
    return detail;
  }

  const message = normalizeErrorValue(data.message);
  if (message) {
    return message;
  }

  const error = normalizeErrorValue(data.error);
  if (error) {
    return error;
  }

  return fallback;
}

function normalizeErrorValue(value) {
  if (!value) {
    return "";
  }
  if (typeof value === "string") {
    return value.trim();
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => normalizeErrorValue(item))
      .filter(Boolean);
    return parts.join(" ").trim();
  }
  if (typeof value === "object") {
    if (typeof value.msg === "string" && value.msg.trim()) {
      return value.msg.trim();
    }
    if (typeof value.message === "string" && value.message.trim()) {
      return value.message.trim();
    }
  }
  return "";
}

function createNode(tagName, className = "") {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  return node;
}

function createTextNode(tagName, className, text) {
  const node = createNode(tagName, className);
  node.textContent = text;
  return node;
}
