// Application JavaScript pour IAction

class IActionApp {
  constructor() {
    this.isCapturing = false;
    this.detections = [];
    this.statusInterval = null;
    this.videoUpdateInterval = null;
    // Gestion du niveau de logs (UI + console)
    this.logLevels = { error: 0, warning: 1, info: 2, success: 2, debug: 3 };
    this.logLevel = "info";

    this.editDetectionId = null; // id de la détection en cours d'édition

    this.init();
  }

  init() {
    this.initLogLevelFromUrl();
    this.setupEventListeners();
    this.loadDetections();
    this.checkCaptureStatus(); // Vérifier si une capture est déjà en cours
    this.startStatusUpdates();
    this.addLog("Application initialisée", "info");
  }

  setupEventListeners() {
    // Contrôles de capture unique
    document
      .getElementById("start-capture")
      .addEventListener("click", () => this.startCapture());
    document
      .getElementById("stop-capture")
      .addEventListener("click", () => this.stopCapture());

    // Contrôles multi-caméras
    document
      .getElementById("start-multi-capture")
      .addEventListener("click", () => this.startMultiCapture());
    document
      .getElementById("stop-multi-capture")
      .addEventListener("click", () => this.stopMultiCapture());

    // Détections
    document
      .getElementById("add-detection")
      .addEventListener("click", () => this.showAddDetectionModal());
    document
      .getElementById("save-detection")
      .addEventListener("click", () => this.saveDetection());

    // Journaux UI supprimés: aucun binding nécessaire

    // Flux vidéo principal
    const toggleMainStream = document.getElementById("toggle-main-stream");
    if (toggleMainStream) {
      toggleMainStream.addEventListener("click", () =>
        this.toggleMainVideoStream()
      );
    }

    // Modal
    this.addDetectionModal = new bootstrap.Modal(
      document.getElementById("addDetectionModal")
    );

    // Suivi des analyses
    this.lastAnalysisTime = 0;

    // Suivi du flux vidéo
    this.isVideoStreamVisible = false;
    this.captureInProgress = false;

    // État des caméras multiples
    this.activeCameras = {};
    this.camerasConfig = [];
  }

  async loadDetections() {
    try {
      const response = await fetch("/api/detections");
      const detections = await response.json();

      this.detections = detections;
      this.updateDetectionsList();

      this.addLog(`${detections.length} détections chargées`, "info");
    } catch (error) {
      this.addLog(
        `Erreur lors du chargement des détections: ${error.message}`,
        "error"
      );
    }
  }

  updateDetectionsList() {
    const container = document.getElementById("detections-list");

    if (this.detections.length === 0) {
      container.innerHTML =
        '<p class="text-muted">Aucune détection configurée</p>';
      return;
    }

    container.innerHTML = "";

    this.detections.forEach((detection) => {
      const item = document.createElement("div");
      item.className = "detection-item";

      // Icône webhook si configuré
      const webhookIcon = detection.webhook_url
        ? '<i class="bi bi-link-45deg text-primary" title="Webhook configuré"></i> '
        : "";

      item.innerHTML = `
                <div class="detection-name">${webhookIcon}${detection.name}</div>
                <div class="detection-phrase">${detection.phrase}</div>
                <div class="detection-controls">
                    <span class="badge status-badge bg-secondary">Inactif</span>
                    <button class="btn btn-outline-secondary btn-sm me-2" onclick="app.editDetection('${detection.id}')">
                        <i class="bi bi-pencil"></i>
                    </button>
                    <button class="btn btn-danger btn-sm" onclick="app.removeDetection('${detection.id}')">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            `;
      item.id = `detection-${detection.id}`;
      container.appendChild(item);
    });
  }

  async checkCaptureStatus() {
    try {
      const response = await fetch("/api/capture_status");
      if (response.ok) {
        const data = await response.json();
        if (data.is_capturing) {
          this.isCapturing = true;
          this.captureInProgress = true;
          this.updateCaptureControls();
          this.showToggleButton();
          this.addLog(
            'Capture détectée en cours - Bouton "Voir le flux live" disponible',
            "success"
          );
        } else {
          console.log("Aucune capture en cours au démarrage");
        }
      }
    } catch (error) {
      console.log(
        "Impossible de vérifier l'\u00e9tat de capture:",
        error.message
      );
    }
  }

  async checkCaptureStatusUpdate() {
    try {
      const response = await fetch("/api/capture_status");
      if (response.ok) {
        const data = await response.json();
        const wasCapturing = this.captureInProgress;

        if (data.is_capturing && !wasCapturing) {
          // Capture vient de démarrer
          this.isCapturing = true;
          this.captureInProgress = true;
          this.updateCaptureControls();
          this.showToggleButton();
          this.showCaptureLoading(false); // Masquer le spinner
          this.addLog("✅ Capture détectée - Interface mise à jour", "success");
        } else if (!data.is_capturing && wasCapturing) {
          // Capture vient de s'arrêter
          this.isCapturing = false;
          this.captureInProgress = false;
          this.updateCaptureControls();
          this.hideToggleButton();
          this.stopVideoStream();
          this.showCaptureLoading(false); // Masquer le spinner
          this.addLog("⚠️ Capture arrêtée - Interface mise à jour", "info");
        }
      }
    } catch (error) {
      // Erreur silencieuse pour éviter le spam
      console.debug("Vérification d'\u00e9tat de capture:", error.message);
    }
  }

  async startCapture() {
    // Récupérer la configuration serveur (mode + RTSP)
    let captureMode = "rtsp";
    let rtspUrl = null;
    try {
      const configResponse = await fetch("/api/config");
      const config = await configResponse.json();
      captureMode = config.capture_mode || "rtsp";
      rtspUrl = config.rtsp_url || null;
    } catch (error) {
      this.addLog(
        "Erreur lors de la récupération de la configuration",
        "error"
      );
      return;
    }

    // Validation selon le mode
    if (captureMode === "rtsp" && !rtspUrl) {
      this.addLog(
        "URL RTSP non configurée. Veuillez configurer l'URL dans la section Administration.",
        "warning"
      );
      return;
    }

    // Afficher le spinner
    this.showCaptureLoading(true);
    const startMsg =
      captureMode === "rtsp"
        ? `Démarrage de la capture RTSP : ${rtspUrl}`
        : "Démarrage de la capture via Home Assistant (Polling)";
    this.addLog(startMsg, "info");

    try {
      const response = await fetch("/api/start_capture", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(
          captureMode === "rtsp"
            ? { source: rtspUrl, type: "rtsp", rtsp_url: rtspUrl }
            : { type: "ha_polling" }
        ),
      });

      const result = await response.json();

      if (response.ok) {
        this.isCapturing = true;
        this.captureInProgress = true;
        this.updateCaptureControls();
        this.showToggleButton();
        // Ne pas démarrer automatiquement le flux vidéo

        const message = result.message || `Capture démarrée avec succès.`;
        this.addLog(message, "success");

        if (result.camera) {
          this.displayCameraInfo(result.camera);
        }
      } else {
        const errorMsg =
          result.error || "Erreur inconnue lors du démarrage de la capture.";
        this.addLog(`Erreur: ${errorMsg}`, "error");
      }
    } catch (error) {
      console.error("Erreur de démarrage de la capture:", error);
      this.addLog(
        `Erreur critique lors du démarrage: ${error.message}`,
        "error"
      );
    } finally {
      // Masquer le spinner dans tous les cas
      this.showCaptureLoading(false);
    }
  }

  async stopCapture() {
    try {
      const response = await fetch("/api/stop_capture", {
        method: "POST",
      });

      const result = await response.json();

      if (response.ok) {
        this.isCapturing = false;
        this.captureInProgress = false;
        this.updateCaptureControls();
        this.hideToggleButton();
        this.stopVideoStream();
        this.showCaptureLoading(false); // S'assurer que le spinner est masqué
        this.addLog("Capture arrêtée", "info");
      } else {
        this.addLog(`Erreur: ${result.error}`, "error");
      }
    } catch (error) {
      this.addLog(`Erreur lors de l'arrêt: ${error.message}`, "error");
    } finally {
      // S'assurer que le spinner est masqué dans tous les cas
      this.showCaptureLoading(false);
    }
  }

  updateCaptureControls() {
    const startBtn = document.getElementById("start-capture");
    const stopBtn = document.getElementById("stop-capture");

    if (this.isCapturing) {
      startBtn.disabled = true;
      stopBtn.disabled = false;
    } else {
      startBtn.disabled = false;
      stopBtn.disabled = true;
    }
  }

  showCaptureLoading(show) {
    const startBtn = document.getElementById("start-capture");
    const startContent = document.getElementById("start-capture-content");
    const startLoading = document.getElementById("start-capture-loading");

    // Vérifier que les éléments existent
    if (!startBtn || !startContent || !startLoading) {
      console.warn("Eléments du bouton de capture non trouvés");
      return;
    }

    if (show) {
      // Afficher le spinner et désactiver le bouton
      startBtn.disabled = true;
      startContent.classList.add("d-none");
      startLoading.classList.remove("d-none");
      console.debug("Spinner affiché");
    } else {
      // Masquer le spinner et réactiver le bouton si pas en capture
      startContent.classList.remove("d-none");
      startLoading.classList.add("d-none");

      // Réactiver le bouton seulement si pas en capture
      if (!this.isCapturing) {
        startBtn.disabled = false;
      }
      console.debug("Spinner masqué, isCapturing:", this.isCapturing);
    }
  }

  startVideoStream() {
    const videoStream = document.getElementById("video-stream");
    const captureReady = document.getElementById("capture-ready");
    const noCapture = document.getElementById("no-capture");

    // Ajouter un paramètre unique pour éviter la mise en cache
    videoStream.src = "/video_feed?" + new Date().getTime();
    videoStream.style.display = "block";
    if (captureReady) captureReady.style.display = "none";
    if (noCapture) noCapture.style.display = "none";

    // Supprimer la mise à jour périodique du flux vidéo qui cause des reconnexions
    // Le flux MJPEG est déjà en streaming continu et n'a pas besoin d'être rechargé
    if (this.videoUpdateInterval) {
      clearInterval(this.videoUpdateInterval);
    }

    // Ajouter un gestionnaire d'erreur pour le flux vidéo
    videoStream.onerror = () => {
      console.log(
        "Erreur de chargement du flux vidéo, tentative de reconnexion..."
      );
      setTimeout(() => {
        if (this.isCapturing) {
          videoStream.src = "/video_feed?" + new Date().getTime();
        }
      }, 2000);
    };
  }

  stopVideoStream() {
    const videoStream = document.getElementById("video-stream");
    const captureReady = document.getElementById("capture-ready");
    const toggleText = document.getElementById("toggle-video-text");
    const toggleIcon = document
      .getElementById("toggle-video-stream")
      .querySelector("i");

    if (this.videoUpdateInterval) {
      clearInterval(this.videoUpdateInterval);
      this.videoUpdateInterval = null;
    }

    videoStream.style.display = "none";
    videoStream.src = "";

    // Si capture en cours, afficher l'état "capture ready", sinon l'état par défaut
    if (this.captureInProgress) {
      captureReady.style.display = "block";
    }

    // Réinitialiser l'état du bouton toggle
    this.isVideoStreamVisible = false;
    if (toggleText) {
      toggleText.textContent = "Voir le flux live";
      toggleIcon.className = "bi bi-eye";
    }
  }

  showAddDetectionModal() {
    // Basculer en mode ajout
    this.editDetectionId = null;
    document.getElementById("detection-name").value = "";
    document.getElementById("detection-phrase").value = "";
    const webhook = document.getElementById("detection-webhook");
    if (webhook) webhook.value = "";
    const title = document.getElementById("detection-modal-title");
    if (title) title.textContent = "Ajouter une Détection";
    const saveBtn = document.getElementById("save-detection");
    if (saveBtn) saveBtn.textContent = "Sauvegarder";
    this.addDetectionModal.show();
  }

  editDetection(detectionId) {
    const det = this.detections.find((d) => d.id === detectionId);
    if (!det) {
      this.addLog("Détection introuvable pour édition", "error");
      return;
    }
    this.showEditDetectionModal(det);
  }

  showEditDetectionModal(detection) {
    this.editDetectionId = detection.id;
    document.getElementById("detection-name").value = detection.name || "";
    document.getElementById("detection-phrase").value = detection.phrase || "";
    const webhook = document.getElementById("detection-webhook");
    if (webhook) webhook.value = detection.webhook_url || "";
    const title = document.getElementById("detection-modal-title");
    if (title) title.textContent = "Modifier une Détection";
    const saveBtn = document.getElementById("save-detection");
    if (saveBtn) saveBtn.textContent = "Mettre à jour";
    this.addDetectionModal.show();
  }

  async saveDetection() {
    const name = document.getElementById("detection-name").value.trim();
    const phrase = document.getElementById("detection-phrase").value.trim();
    const webhookUrl = document
      .getElementById("detection-webhook")
      .value.trim();

    // Validation: création vs édition
    if (!this.editDetectionId) {
      // Création: nom et phrase requis
      if (!name || !phrase) {
        this.addLog("Nom et phrase requis pour la détection", "warning");
        return;
      }
    } else {
      // Édition: au moins un des deux champs
      if (!name && !phrase && typeof webhookUrl === "undefined") {
        this.addLog("Aucun changement détecté", "warning");
        return;
      }
    }

    // Valider l'URL webhook si fournie
    if (webhookUrl && !webhookUrl.match(/^https?:\/\/.+/)) {
      this.addLog(
        "URL webhook invalide (doit commencer par http:// ou https://)",
        "warning"
      );
      return;
    }

    try {
      // Corps de requête: en édition, envoyer uniquement ce qui est pertinent
      let requestBody = {};
      if (!this.editDetectionId || name) requestBody.name = name;
      if (!this.editDetectionId || phrase) requestBody.phrase = phrase;
      if (typeof webhookUrl !== "undefined")
        requestBody.webhook_url = webhookUrl;

      let url = "/api/detections";
      let method = "POST";
      let actionLog = "ajoutée";
      if (this.editDetectionId) {
        url = `/api/detections/${this.editDetectionId}`;
        method = "PATCH"; // compatible et souvent mieux accepté par proxys
        actionLog = "mise à jour";
      }

      const response = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify(requestBody),
      });

      // Tenter de récupérer du JSON, sinon texte brut
      const contentType = (
        response.headers.get("content-type") || ""
      ).toLowerCase();
      let result = null;
      let rawText = "";
      if (contentType.includes("application/json")) {
        try {
          result = await response.json();
        } catch (e) {
          // Fallback si le serveur renvoie du HTML mal typé en JSON
          rawText = await response.text();
          try {
            result = JSON.parse(rawText);
          } catch (_) {}
        }
      } else {
        rawText = await response.text();
        try {
          result = JSON.parse(rawText);
        } catch (_) {}
      }

      if (response.ok) {
        this.addDetectionModal.hide();
        await this.loadDetections();
        this.editDetectionId = null;
        this.addLog(`Détection ${actionLog}: ${name}`, "success");
        // Réinitialiser le formulaire
        document.getElementById("detection-form").reset();
      } else {
        const errMsg =
          result && result.error
            ? result.error
            : rawText
            ? rawText.substring(0, 200)
            : response.statusText;
        this.addLog(`Erreur: ${errMsg}`, "error");
      }
    } catch (error) {
      this.addLog(`Erreur lors de la sauvegarde: ${error.message}`, "error");
    }
  }

  async removeDetection(detectionId) {
    if (!confirm("Êtes-vous sûr de vouloir supprimer cette détection ?")) {
      return;
    }

    try {
      const response = await fetch(`/api/detections/${detectionId}`, {
        method: "DELETE",
      });

      const result = await response.json();

      if (response.ok) {
        this.loadDetections();
        this.addLog("Détection supprimée", "info");
      } else {
        this.addLog(`Erreur: ${result.error}`, "error");
      }
    } catch (error) {
      this.addLog(`Erreur lors de la suppression: ${error.message}`, "error");
    }
  }

  startStatusUpdates() {
    // Mise à jour rapide pour réactivité en temps réel
    this.statusInterval = setInterval(() => {
      this.updateSensorValues();
    }, 1000); // 1 seconde pour meilleure réactivité
  }

  async updateSensorValues() {
    try {
      // Utiliser l'endpoint léger pour les métriques
      const response = await fetch("/api/metrics");
      if (response.ok) {
        const data = await response.json();

        // Mettre à jour les indicateurs de temps d'analyse
        this.updateAnalysisTimeIndicators(data);
      }

      // Vérifier l'état de capture périodiquement pour détecter les changements
      await this.checkCaptureStatusUpdate();
    } catch (error) {
      console.error(
        "Erreur lors de la récupération des valeurs des capteurs:",
        error
      );
    }
  }

  updateAnalysisTimeIndicators(statusData) {
    const duration = statusData.last_analysis_duration;
    const analysisTime = statusData.last_analysis_time;
    const isValidDuration = duration && duration > 0;

    // Détecter une nouvelle analyse
    if (analysisTime && analysisTime !== this.lastAnalysisTime) {
      this.lastAnalysisTime = analysisTime;
      // Ajouter un effet visuel pour indiquer une nouvelle analyse
      const fpsElement = document.getElementById("analysis-fps");
      fpsElement.style.color = "#28a745"; // Vert pour nouvelle donnée
      setTimeout(() => (fpsElement.style.color = ""), 1000); // Retour normal après 1s
    }

    // Mise à jour optimisée en une seule fois
    document.getElementById("analysis-fps").textContent = isValidDuration
      ? (1 / duration).toFixed(2)
      : "0.00";
    document.getElementById("analysis-duration").textContent = isValidDuration
      ? duration.toFixed(2)
      : "0.00";

    // Nouveaux indicateurs: FPS total et intervalle total
    const totalFps = Number(statusData.analysis_total_fps || 0);
    const totalInterval = Number(statusData.analysis_total_interval || 0);
    const totalFpsElem = document.getElementById("analysis-total-fps");
    const totalIntervalElem = document.getElementById(
      "analysis-total-interval"
    );
    if (totalFpsElem)
      totalFpsElem.textContent = Number.isFinite(totalFps)
        ? totalFps.toFixed(2)
        : "0.00";
    if (totalIntervalElem)
      totalIntervalElem.textContent = Number.isFinite(totalInterval)
        ? totalInterval.toFixed(2)
        : "0.00";
  }

  addLog(message, type = "info") {
    // UI logs supprimés: sortie console uniquement selon niveau
    if (!this.shouldLog(type)) return;
    this.consoleLog(type, message);
  }

  initLogLevelFromUrl() {
    try {
      const p = new URLSearchParams(window.location.search);
      if (p.has("log")) {
        const lvl = (p.get("log") || "").toLowerCase();
        if (lvl in this.logLevels) {
          localStorage.setItem("LOG_LEVEL", lvl);
        }
      }
      const stored = (
        localStorage.getItem("LOG_LEVEL") || "info"
      ).toLowerCase();
      this.logLevel = stored in this.logLevels ? stored : "info";
    } catch (_) {
      this.logLevel = "info";
    }
  }

  shouldLog(type) {
    const lvl = this.logLevels[(type || "info").toLowerCase()] ?? 2;
    const current = this.logLevels[this.logLevel] ?? 2;
    return lvl <= current;
  }

  consoleLog(type, message) {
    const styles = {
      success: "color: #198754;", // bootstrap green
      info: "color: #0dcaf0;", // bootstrap cyan
      warning: "color: #ffc107;", // bootstrap yellow
      error: "color: #dc3545;", // bootstrap red
      debug: "color: #6c757d;", // bootstrap secondary
    };
    const style = styles[type] || "";
    const prefix = `[IAction]`;
    const line = `%c${prefix} ${type.toUpperCase()}:`;
    if (type === "error") console.error(line, style, message);
    else if (type === "warning") console.warn(line, style, message);
    else if (type === "debug") console.debug(line, style, message);
    else console.log(line, style, message);
  }

  // Fonction supprimée - plus de détection USB nécessaire

  displayCameraInfo(camera) {
    if (!camera || camera.type !== "rtsp") return;

    const infoMessage = ["Type: Caméra RTSP"];

    if (camera.test_status) {
      const statusText = {
        online: "En ligne",
        offline: "Hors ligne",
        error: "Erreur de connexion",
        not_configured: "Non configurée",
      };
      infoMessage.push(
        `Statut: ${statusText[camera.test_status] || "Inconnu"}`
      );
    }

    this.addLog(`Infos caméra - ${infoMessage.join(", ")}`, "info");
  }

  showToggleButton() {
    const toggleButton = document.getElementById("toggle-video-stream");
    const noCapture = document.getElementById("no-capture");
    const captureReady = document.getElementById("capture-ready");

    toggleButton.style.display = "block";
    noCapture.style.display = "none";
    captureReady.style.display = "block";

    // Réinitialiser l'état du flux vidéo
    this.isVideoStreamVisible = false;

    this.addLog('Bouton "Voir le flux live" disponible', "info");
  }

  hideToggleButton() {
    const toggleButton = document.getElementById("toggle-video-stream");
    const noCapture = document.getElementById("no-capture");
    const captureReady = document.getElementById("capture-ready");

    toggleButton.style.display = "none";
    noCapture.style.display = "block";
    captureReady.style.display = "none";

    // Réinitialiser l'état du flux vidéo
    this.isVideoStreamVisible = false;
  }

  toggleVideoStream() {
    const videoStream = document.getElementById("video-stream");
    const captureReady = document.getElementById("capture-ready");
    const toggleText = document.getElementById("toggle-video-text");
    const toggleIcon = document
      .getElementById("toggle-video-stream")
      .querySelector("i");

    // Vérifier si une capture est en cours
    if (!this.captureInProgress) {
      this.addLog(
        " Aucune capture en cours. Démarrez d'abord une capture RTSP.",
        "warning"
      );
      return;
    }

    if (this.isVideoStreamVisible) {
      // Masquer le flux - retour à l'état "capture ready"
      videoStream.style.display = "none";
      videoStream.src = "";
      captureReady.style.display = "block";

      toggleText.textContent = "Voir le flux live";
      toggleIcon.className = "bi bi-eye";
      this.isVideoStreamVisible = false;

      console.log("Flux vidéo masqué");
    } else {
      // Afficher le flux - masquer l'état "capture ready"
      videoStream.src = "/video_feed?" + new Date().getTime();
      videoStream.style.display = "block";
      captureReady.style.display = "none";

      toggleText.textContent = "Masquer le flux";
      toggleIcon.className = "bi bi-eye-slash";
      this.isVideoStreamVisible = true;

      console.log("Flux vidéo affiché");
    }
  }

  // === MÉTHODES MULTI-CAMÉRAS ===

  async startMultiCapture() {
    try {
      this.addLog("🎬 Démarrage de la capture multi-caméras...", "info");

      // Charger la configuration des caméras depuis l'admin
      await this.loadCamerasConfiguration();

      const btn = document.getElementById("start-multi-capture");
      btn.disabled = true;
      btn.innerHTML =
        '<span class="spinner-border spinner-border-sm me-2"></span>Démarrage...';

      let allCameras = [];

      // Ajouter la caméra principale
      allCameras.push({
        id: "main",
        name: "Caméra Principale",
        mode: "rtsp",
        // L'URL RTSP sera prise depuis DEFAULT_RTSP_URL
      });

      // Ajouter les caméras configurées
      allCameras = allCameras.concat(this.camerasConfig);

      if (allCameras.length === 1) {
        this.addLog(
          "ℹ️ Seule la caméra principale sera démarrée. Configurez des caméras supplémentaires dans l'administration.",
          "info"
        );
      }

      // Démarrer toutes les caméras
      const response = await fetch("/api/admin/cameras/start_multiple", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cameras: allCameras }),
      });

      const result = await response.json();

      if (result.success) {
        // Mettre à jour l'interface pour chaque caméra
        this.setupCamerasGrid(result.results);

        document.getElementById("start-multi-capture").disabled = true;
        document.getElementById("stop-multi-capture").disabled = false;

        const successCount = result.results.filter((r) => r.success).length;
        this.addLog(
          `✅ ${successCount}/${result.results.length} caméra(s) démarrée(s)`,
          "success"
        );
      } else {
        this.addLog(`❌ Erreur: ${result.error}`, "error");
      }
    } catch (error) {
      this.addLog(
        `❌ Erreur lors du démarrage multi-caméras: ${error.message}`,
        "error"
      );
    } finally {
      const btn = document.getElementById("start-multi-capture");
      btn.disabled = false;
      btn.innerHTML =
        '<i class="bi bi-play-circle"></i> Démarrer Multi-Capture';
    }
  }

  async stopMultiCapture() {
    try {
      this.addLog("⏹️ Arrêt de la capture multi-caméras...", "info");

      const btn = document.getElementById('stop-multi-capture');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Arrêt...';

      const response = await fetch("/api/stop_capture", { 
        method: "POST",
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}) // Arrêter toutes les caméras
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const result = await response.json();

      if (result.success) {
        // Réinitialiser l'interface
        this.resetCamerasGrid();

        document.getElementById("start-multi-capture").disabled = false;
        document.getElementById("stop-multi-capture").disabled = true;

        this.addLog("✅ Capture multi-caméras arrêtée", "success");
        this.addLog(`ℹ️ ${result.message}`, "info");
      } else {
        this.addLog(`❌ Erreur lors de l'arrêt: ${result.error}`, "error");
      }
    } catch (error) {
      this.addLog(
        `❌ Erreur lors de l'arrêt multi-caméras: ${error.message}`,
        "error"
      );
      console.error('Erreur stopMultiCapture:', error);
    } finally {
      // Restaurer le bouton même en cas d'erreur
      const startBtn = document.getElementById('start-multi-capture');
      const stopBtn = document.getElementById('stop-multi-capture');
      
      startBtn.disabled = false;
      startBtn.innerHTML = '<i class="bi bi-play-circle"></i> Démarrer Multi-Capture';
      
      stopBtn.disabled = true;
      stopBtn.innerHTML = '<i class="bi bi-stop-circle"></i> Arrêter Multi-Capture';
    }
  }

  async loadCamerasConfiguration() {
    try {
      // Charger depuis le localStorage (temporaire) ou API
      const savedCameras = localStorage.getItem("additional_cameras");

      if (savedCameras) {
        const cameras = JSON.parse(savedCameras);
        this.camerasConfig = cameras.map((camera) => {
          const cameraId = camera[`${camera.id}_id`] || camera.id;
          const mode = camera[`${camera.id}_mode`] || "rtsp";
          const name = camera[`${camera.id}_name`] || `Caméra ${cameraId}`;

          let config = {
            id: cameraId,
            name: name,
            mode: mode,
          };

          if (mode === "rtsp") {
            config.rtsp_url = camera[`${camera.id}_rtsp_url`];
            config.rtsp_username = camera[`${camera.id}_rtsp_username`];
            config.rtsp_password = camera[`${camera.id}_rtsp_password`];
          } else if (mode === "ha_polling") {
            config.ha_entity = camera[`${camera.id}_ha_entity`];
            config.ha_attr = camera[`${camera.id}_ha_attr`];
            config.ha_interval = camera[`${camera.id}_ha_interval`];
          }

          return config;
        });
      } else {
        this.camerasConfig = [];
      }

      this.addLog(
        `Configuration chargée: ${this.camerasConfig.length} caméra(s) configurée(s)`,
        "info"
      );
    } catch (error) {
      this.addLog(
        `❌ Erreur chargement configuration: ${error.message}`,
        "error"
      );
      this.camerasConfig = [];
    }
  }

  setupCamerasGrid(results) {
    const grid = document.getElementById("cameras-grid");
    const mainContainer = document.getElementById("main-camera-container");

    // Nettoyer les caméras supplémentaires existantes
    const existingCameras = grid.querySelectorAll("[data-camera-id]");
    existingCameras.forEach((cam) => cam.remove());

    // Ajouter chaque caméra à la grille
    results.forEach((result) => {
      if (result.camera_id === "main") {
        // Caméra principale
        this.updateMainCameraStatus(result.success, result.message);
        return;
      }

      this.addCameraToGrid(result);
    });
  }

  addCameraToGrid(cameraResult) {
    const grid = document.getElementById("cameras-grid");
    const cameraDiv = document.createElement("div");
    cameraDiv.className = "col-md-6";
    cameraDiv.setAttribute("data-camera-id", cameraResult.camera_id);

    const statusBadge = cameraResult.success
      ? '<span class="badge bg-success">Active</span>'
      : '<span class="badge bg-danger">Erreur</span>';

    cameraDiv.innerHTML = `
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <small class="text-muted">
                        <i class="bi bi-camera-video"></i> ${
                          cameraResult.camera_name || cameraResult.camera_id
                        }
                    </small>
                    <div>
                        ${statusBadge}
                        <button class="btn btn-outline-primary btn-sm ms-1" onclick="app.toggleCameraStream('${
                          cameraResult.camera_id
                        }')" ${!cameraResult.success ? "disabled" : ""}>
                            <i class="bi bi-eye"></i>
                        </button>
                    </div>
                </div>
                <div class="card-body p-2 text-center">
                    <div class="camera-video-container">
                        <img class="camera-stream img-fluid rounded" style="display: none; max-height: 250px;" alt="Flux ${
                          cameraResult.camera_id
                        }">
                        
                        <div class="camera-placeholder py-4">
                            <div class="mb-3">
                                <i class="bi bi-${
                                  cameraResult.success
                                    ? "eye text-success"
                                    : "exclamation-triangle text-danger"
                                }" style="font-size: 3rem;"></i>
                            </div>
                            <h6 class="${
                              cameraResult.success
                                ? "text-success"
                                : "text-danger"
                            }">${cameraResult.message}</h6>
                            ${
                              cameraResult.success
                                ? '<small class="text-muted">Cliquez sur l\'œil pour voir le flux</small>'
                                : ""
                            }
                        </div>
                    </div>
                </div>
            </div>
        `;

    grid.appendChild(cameraDiv);
  }

  updateMainCameraStatus(isActive, message = null) {
    const statusBadge = document.getElementById("main-camera-status");
    const toggleBtn = document.getElementById("toggle-main-stream");
    const noCapture = document.getElementById("main-no-capture");
    const captureReady = document.getElementById("main-capture-ready");

    if (isActive) {
      statusBadge.textContent = "Active";
      statusBadge.className = "badge bg-success";
      toggleBtn.style.display = "";
      noCapture.style.display = "none";
      captureReady.style.display = "block";

      if (message) {
        // Optionnel: afficher le message de succès
        const readyText = captureReady.querySelector("h6");
        if (readyText) readyText.textContent = message;
      }
    } else {
      statusBadge.textContent = message || "Arrêtée";
      statusBadge.className = "badge bg-secondary";
      toggleBtn.style.display = "none";
      noCapture.style.display = "block";
      captureReady.style.display = "none";
    }
  }

  resetCamerasGrid() {
    this.addLog("🔄 Réinitialisation de la grille des caméras...", "info");
    
    // Supprimer toutes les caméras supplémentaires
    const grid = document.getElementById("cameras-grid");
    const existingCameras = grid.querySelectorAll("[data-camera-id]");
    this.addLog(`🗑️ Suppression de ${existingCameras.length} caméra(s) supplémentaire(s)`, "info");
    existingCameras.forEach((cam) => cam.remove());

    // Réinitialiser la caméra principale
    this.updateMainCameraStatus(false, "Arrêtée");

    // Masquer les flux
    const mainStream = document.getElementById("main-video-stream");
    if (mainStream) {
      mainStream.style.display = "none";
      mainStream.src = "";
    }
    
    // Réinitialiser l'état interne
    this.activeCameras = {};
    
    this.addLog("✅ Grille des caméras réinitialisée", "success");
  }

  toggleMainVideoStream() {
    const videoStream = document.getElementById("main-video-stream");
    const captureReady = document.getElementById("main-capture-ready");
    const toggleBtn = document.getElementById("toggle-main-stream");
    const icon = toggleBtn.querySelector("i");

    if (videoStream.style.display === "none") {
      // Afficher le flux principal
      videoStream.src = "/video_feed/main?" + new Date().getTime();
      videoStream.style.display = "block";
      captureReady.style.display = "none";
      icon.className = "bi bi-eye-slash";
    } else {
      // Masquer le flux principal
      videoStream.style.display = "none";
      videoStream.src = "";
      captureReady.style.display = "block";
      icon.className = "bi bi-eye";
    }
  }

  toggleCameraStream(cameraId) {
    const cameraContainer = document.querySelector(
      `[data-camera-id="${cameraId}"]`
    );
    if (!cameraContainer) return;

    const videoStream = cameraContainer.querySelector(".camera-stream");
    const placeholder = cameraContainer.querySelector(".camera-placeholder");
    const toggleBtn = cameraContainer.querySelector("button");
    const icon = toggleBtn.querySelector("i");

    if (videoStream.style.display === "none") {
      // Afficher le flux
      videoStream.src = `/video_feed/${cameraId}?` + new Date().getTime();
      videoStream.style.display = "block";
      placeholder.style.display = "none";
      icon.className = "bi bi-eye-slash";
    } else {
      // Masquer le flux
      videoStream.style.display = "none";
      videoStream.src = "";
      placeholder.style.display = "block";
      icon.className = "bi bi-eye";
    }
  }
}

// Initialiser l'application
const app = new IActionApp();
