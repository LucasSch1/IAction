from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
import cv2
import threading
import time
import base64
import json
import os
import logging
import sys
import re
import socket
import errno
import numpy as np
from dotenv import load_dotenv
from services.camera_service import CameraService
from services.ai_service import AIService
from services.mqtt_service import get_mqtt_instance, MQTTService
from services.detection_service import DetectionService
from services.ha_service import HAService

#TEST Introduction classe
class CameraContext:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.current_frame = None
        self.is_capturing = False
        self.analysis_in_progress = False
        self.last_analysis_time = 0
        self.last_analysis_duration = 0
        self.last_analysis_total_interval = 0
        self.ai_consecutive_failures = 0

camera_contexts = {}



# Charger les variables d'environnement
load_dotenv(override=True)  # Forcer le remplacement des variables d'environnement existantes
# Format de logs unifié pour le CLI + niveau via env LOG_LEVEL
log_level_name = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

def _sanitize_env_value(value, key: str) -> str:
    """Normalize values written to .env to avoid spaces breaking Docker env parsing.
    - Trim whitespace
    - Remove surrounding single/double quotes
    - Replace internal whitespace with underscores for most keys
    Some keys are exempt (URLs, tokens, passwords) where spaces are either invalid
    or should not be altered semantically.
    """
    try:
        if value is None:
            return ''
        v = str(value).strip()
        # Remove surrounding quotes if present
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]

        # Keys that should not have their spaces converted
        exempt_keys = {
            'DEFAULT_RTSP_URL', 'LMSTUDIO_URL', 'OLLAMA_URL', 'OPENAI_API_KEY',
            'MQTT_PASSWORD', 'RTSP_PASSWORD', 'HA_TOKEN', 'HA_BASE_URL'
        }

        if key not in exempt_keys:
            # Collapse any whitespace (spaces, tabs) into single underscores
            v = re.sub(r"\s+", "_", v)

        return v
    except Exception:
        return '' if value is None else str(value)

def is_running_in_docker() -> bool:
    """Detect if we're running inside a Docker container.
    Checks /.dockerenv presence or IN_DOCKER env var.
    """
    try:
        if os.path.exists('/.dockerenv'):
            return True
        return str(os.environ.get('IN_DOCKER', '')).lower() in ('1', 'true', 'yes')
    except Exception:
        return False

def _wait_until_bind_possible(host: str, port: int, timeout: float = 10.0) -> bool:
    """Attend jusqu'à ce qu'un bind(host, port) soit possible (port vraiment libéré)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.close()
            return True
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(0.2)
    return False

def _run_web_server_with_retry(host: str = '0.0.0.0', port: int = 5002, debug: bool = False, max_attempts: int = 8):
    """Lance Flask avec une stratégie de retry robuste si le port est encore occupé.
    - Pré-vérifie la disponibilité du port par un bind test.
    - Retrie en cas d'OSError (EADDRINUSE) et de SystemExit issus de werkzeug.
    """
    delay = 0.4
    attempt = 1
    while attempt <= max_attempts:
        mode = 'DEBUG' if debug else 'PRODUCTION'
        if attempt > 1:
            logger.info(f"Nouvelle tentative de démarrage du serveur (essai {attempt}/{max_attempts}) en mode {mode}...")

        # Pré-bind: vérifier que le port est libre
        prebind_ok = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.close()
            prebind_ok = True
        except OSError as e:
            if getattr(e, 'errno', None) in (errno.EADDRINUSE, 10048):
                if attempt < max_attempts:
                    logger.warning(f"Port {port} occupé (pré-vérification), attente {delay:.1f}s avant retry...")
                    time.sleep(delay)
                    delay = min(2.0, delay * 1.6)
                    attempt += 1
                    continue
            # Autre erreur, on tente quand même app.run pour obtenir un message clair
        except Exception:
            # On ne bloque pas sur la pré-vérif
            pass

        try:
            app.run(debug=debug, host=host, port=port, threaded=True, use_reloader=False)
            return
        except OSError as e:
            msg = str(e).lower()
            addr_in_use = (getattr(e, 'errno', None) in (errno.EADDRINUSE, 10048)) or ('address already in use' in msg or ('port' in msg and 'in use' in msg))
            if addr_in_use and attempt < max_attempts:
                logger.warning(f"Port {port} occupé, attente {delay:.1f}s avant retry...")
                time.sleep(delay)
                delay = min(2.0, delay * 1.6)
                attempt += 1
                continue
            raise
        except SystemExit as e:
            # Certains chemins de werkzeug lèvent SystemExit quand le bind échoue
            if attempt < max_attempts:
                logger.warning(f"Échec de démarrage du serveur (SystemExit). Attente {delay:.1f}s avant retry...")
                time.sleep(delay)
                delay = min(2.0, delay * 1.6)
                attempt += 1
                continue
            raise

def _build_restart_args() -> list:
    """Build clean argv for re-exec: keep current flags but force no reloader."""
    try:
        new_args = [a for a in sys.argv[1:] if a != '--no-reloader']
        new_args.append('--no-reloader')
        return [sys.executable, sys.argv[0]] + new_args
    except Exception:
        return [sys.executable, sys.argv[0], '--no-reloader']

def _wait_for_port_to_close(host: str, port: int, timeout: float = 10.0) -> bool:
    """Return True when TCP connect fails (port closed) or timeout reached.
    Polls the given host:port until connection is refused, meaning the previous
    server has released the port.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.3)
            s.connect((host, port))
            # Connection succeeded -> server still up
            s.close()
            time.sleep(0.2)
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            return True
    return False

def _delayed_self_restart(delay_sec: float = 0.3, shutdown_fn=None):
    """Redémarrage robuste par sous-processus (tous environnements).
    - Crée un sous-processus Python (avec IACTION_WAIT_FOR_PID)
    - Arrête proprement le serveur actuel, cleanup, puis quitte le parent
    - Le sous-processus attend que le port soit libéré avant de démarrer
    """
    try:
        time.sleep(delay_sec)
        args = _build_restart_args()
        # Créer un sous-processus, arrêter proprement, puis quitter
        try:
            env = os.environ.copy()
            env['IACTION_WAIT_FOR_PID'] = str(os.getpid())
            logger.info(f"🔁 Redémarrage via nouveau subprocess: {args}")
            import subprocess
            subprocess.Popen(args, close_fds=True, env=env)
        except Exception as e:
            logger.error(f"Échec du lancement du processus enfant: {e}")
        finally:
            if shutdown_fn:
                try:
                    logger.info("🛑 Arrêt du serveur en cours (shutdown werkzeug)...")
                    shutdown_fn()
                except Exception as e:
                    logger.debug(f"shutdown werkzeug ignoré: {e}")
            try:
                cleanup()
            except Exception:
                pass
            os._exit(0)
    except Exception as e:
        logger.error(f"Erreur inattendue pendant le redémarrage différé: {e}")
        try:
            cleanup()
        except Exception:
            pass
        os._exit(0)

def resize_frame_for_analysis(frame):
    """Redimensionne une frame en 720p pour l'analyse IA de manière centralisée"""
    try:
        if frame is None:
            return None
        # Vérifier si déjà en 720p pour éviter un redimensionnement inutile
        height, width = frame.shape[:2]
        if height == 720 and width == 1280:
            return frame
        return cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
    except Exception as e:
        logger.warning(f"Erreur lors du redimensionnement: {e}")
        return frame

app = Flask(__name__)
CORS(app)

# Services globaux
camera_service = CameraService()
ai_service = AIService()
mqtt_service = get_mqtt_instance()  # Utiliser le singleton MQTT
detection_service = DetectionService(ai_service, mqtt_service)

# Variables globales pour compatibilité
current_frame = None
analysis_in_progress = False  # Indique si une analyse est en cours
last_analysis_time = 0  # Timestamp de la dernière analyse terminée
last_analysis_duration = 0  # Durée de la dernière analyse en secondes
last_analysis_total_interval = 0  # Intervalle total entre deux réponses (fin -> fin)
shutting_down = False  # Indicateur d'arrêt global
# Compteur d'échecs IA consécutifs pour arrêt automatique
ai_consecutive_failures = 0

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config')
def get_config():
    """Expose la configuration nécessaire au frontend"""
    config = {
        'rtsp_url': os.getenv('DEFAULT_RTSP_URL', ''),
        'capture_mode': os.getenv('CAPTURE_MODE', 'rtsp'),
        'ha_base_url': os.getenv('HA_BASE_URL', ''),
        'ha_entity_id': os.getenv('HA_ENTITY_ID', ''),
        'ha_image_attr': os.getenv('HA_IMAGE_ATTR', 'entity_picture'),
        'ha_poll_interval': float(os.getenv('HA_POLL_INTERVAL', '1.0')),
    }
    return jsonify(config)

@app.route('/api/cameras')
def get_cameras():
    """Récupère la liste des caméras RTSP disponibles"""
    try:
        cameras = camera_service.get_available_cameras()
        return jsonify({
            'success': True,
            'cameras': cameras,
            'count': len(cameras),
            'rtsp_count': len(cameras)  # Toutes les caméras sont RTSP maintenant
        })
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des caméras: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'cameras': []
        }), 500

@app.route('/api/cameras/refresh', methods=['POST'])
def refresh_cameras():
    """Force la mise à jour de la liste des caméras"""
    try:
        # Effacer le cache
        camera_service.cameras_cache = None
        camera_service.cache_time = 0
        
        # Recharger les caméras
        cameras = camera_service.get_available_cameras()
        
        return jsonify({
            'success': True,
            'message': 'Liste des caméras mise à jour',
            'cameras': cameras,
            'count': len(cameras),
            'rtsp_count': len([c for c in cameras if c['type'] == 'rtsp'])
        })
    except Exception as e:
        logger.error(f"Erreur lors de la mise à jour des caméras: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/cameras/<camera_id>')
def get_camera_info(camera_id):
    """Récupère les informations détaillées d'une caméra"""
    try:
        camera_info = camera_service.get_camera_info(camera_id)
        if camera_info:
            return jsonify({
                'success': True,
                'camera': camera_info
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Caméra non trouvée'
            }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Variable pour suivre les requêtes /api/status
status_request_count = 0
last_status_log_time = 0
status_log_interval = 60  # Intervalle en secondes entre les logs de status

@app.route('/api/status')
def get_status():
    """Récupère les informations de statut de l'analyse pour toutes les caméras"""
    global status_request_count, last_status_log_time
    
    # Incrémenter le compteur de requêtes
    status_request_count += 1
    current_time = time.time()
    
    # Ne logger que périodiquement pour éviter de surcharger les logs
    if current_time - last_status_log_time > status_log_interval:
        logger.info(f"{status_request_count} requêtes /api/status reçues dans les {status_log_interval} dernières secondes")
        status_request_count = 0
        last_status_log_time = current_time
    
    # Collecter le statut de toutes les caméras
    cameras_status = {}
    for camera_id, ctx in camera_contexts.items():
        cameras_status[camera_id] = {
            'last_analysis_time': ctx.last_analysis_time,
            'last_analysis_duration': ctx.last_analysis_duration,
            'analysis_in_progress': ctx.analysis_in_progress,
            'is_capturing': ctx.is_capturing
        }
    
    status = {
        'cameras': cameras_status,
        'active_cameras': len([ctx for ctx in camera_contexts.values() if ctx.is_capturing])
    }
    
    return jsonify(status)

@app.route('/api/metrics')
def get_metrics():
    """Endpoint léger pour les métriques de performance uniquement"""
    global last_analysis_time, last_analysis_duration, last_analysis_total_interval
    
    # Calculer FPS dérivés
    analysis_fps = (1.0 / last_analysis_duration) if last_analysis_duration and last_analysis_duration > 0 else 0
    total_fps = (1.0 / last_analysis_total_interval) if last_analysis_total_interval and last_analysis_total_interval > 0 else 0

    return jsonify({
        'last_analysis_time': last_analysis_time,
        'last_analysis_duration': last_analysis_duration,
        'analysis_fps': analysis_fps,
        'analysis_total_interval': last_analysis_total_interval,
        'analysis_total_fps': total_fps,
        'timestamp': time.time()
    })

@app.route('/api/capture_status')
def get_capture_status():
    """Retourne l'état actuel de la capture pour toutes les caméras"""
    cameras_capture_status = {}
    for camera_id, ctx in camera_contexts.items():
        cameras_capture_status[camera_id] = {
            'is_capturing': ctx.is_capturing,
            'camera_active': camera_id in camera_service.captures
        }
    
    return jsonify({
        'cameras': cameras_capture_status,
        'active_cameras': len([ctx for ctx in camera_contexts.values() if ctx.is_capturing])
    })

#Modification multi-cam
@app.route('/api/start_capture', methods=['POST'])
def start_capture():
    """Démarre la capture vidéo pour une caméra donnée (multi-caméra)"""
    try:
        data = request.json or {}

        camera_id = data.get('source')
        source_type = data.get('type') or os.getenv('CAPTURE_MODE', 'rtsp')
        rtsp_url = data.get('rtsp_url')

        if not camera_id:
            return jsonify({
                'success': False,
                'error': 'source (camera_id) manquant'
            }), 400

        logger.info(f"[{camera_id}] Démarrage capture - type={source_type}, rtsp={rtsp_url}")

        # Créer le contexte caméra si inexistant
        if camera_id not in camera_contexts:
            camera_contexts[camera_id] = CameraContext(camera_id)

        ctx = camera_contexts[camera_id]

        if ctx.is_capturing:
            return jsonify({
                'success': False,
                'error': f'Caméra {camera_id} déjà en cours de capture'
            }), 400

        # =========================
        # ===== MODE RTSP =========
        # =========================
        if source_type == 'rtsp':
            # Déterminer l'URL RTSP à utiliser
            actual_rtsp_url = rtsp_url
            if not actual_rtsp_url:
                # Essayer l'URL par défaut depuis l'environnement
                actual_rtsp_url = os.getenv('DEFAULT_RTSP_URL', '')
                if not actual_rtsp_url:
                    # Si camera_id commence par "rtsp_", c'est peut-être une caméra préconfigurée
                    if camera_id.startswith('rtsp_'):
                        actual_rtsp_url = camera_id  # Le camera_service gérera cela
                    else:
                        return jsonify({
                            'success': False,
                            'error': 'URL RTSP requise (rtsp_url) ou DEFAULT_RTSP_URL dans .env'
                        }), 400
            
            if actual_rtsp_url and not camera_id.startswith('rtsp_'):
                is_valid, message = camera_service.validate_rtsp_url(actual_rtsp_url)
                if not is_valid:
                    return jsonify({
                        'success': False,
                        'error': f'URL RTSP invalide: {message}'
                    }), 400

            ctx.ai_consecutive_failures = 0

            success = camera_service.start_capture(camera_id, actual_rtsp_url, 'rtsp')
            if not success:
                return jsonify({
                    'success': False,
                    'error': 'Impossible de démarrer la capture RTSP'
                }), 400

            ctx.is_capturing = True

            threading.Thread(
                target=capture_loop,
                args=(ctx,),
                daemon=True
            ).start()

            # MQTT par caméra
            try:
                mqtt_service.publish_binary_sensor_state(
                    f"capture_active_{camera_id}", True
                )
            except Exception:
                pass

            camera_info = camera_service.get_camera_info(camera_id)
            return jsonify({
                'success': True,
                'message': f'Capture RTSP démarrée ({camera_id})',
                'camera': camera_info
            })

        # =========================
        # ===== MODE HA POLLING ===
        # =========================
        elif source_type == 'ha_polling':
            if not os.getenv('HA_BASE_URL') or not os.getenv('HA_TOKEN') or not os.getenv('HA_ENTITY_ID'):
                return jsonify({
                    'success': False,
                    'error': 'Configuration HA incomplète'
                }), 400

            ctx.ai_consecutive_failures = 0
            ctx.is_capturing = True

            threading.Thread(
                target=ha_polling_loop,
                args=(ctx,),
                daemon=True
            ).start()

            try:
                mqtt_service.publish_binary_sensor_state(
                    f"capture_active_{camera_id}", True
                )
            except Exception:
                pass

            return jsonify({
                'success': True,
                'message': f'Capture HA Polling démarrée ({camera_id})',
                'camera': None
            })

        else:
            return jsonify({
                'success': False,
                'error': f'Type de capture inconnu: {source_type}'
            }), 400

    except Exception as e:
        logger.exception("Erreur start_capture multi-caméra")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/stop_capture', methods=['POST'])
def stop_capture():
    """Arrête la capture vidéo pour une ou toutes les caméras"""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id')
        
        if camera_id:
            # Arrêter une caméra spécifique
            if camera_id in camera_contexts:
                ctx = camera_contexts[camera_id]
                ctx.is_capturing = False
                camera_service.stop_capture(camera_id)
                
                # Publier l'état de capture (OFF) pour cette caméra
                try:
                    mqtt_service.publish_binary_sensor_state(f'capture_active_{camera_id}', False)
                except Exception:
                    pass
                    
                # Supprimer le contexte
                del camera_contexts[camera_id]
                
                return jsonify({
                    'success': True,
                    'message': f'Capture arrêtée pour {camera_id}'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': f'Caméra {camera_id} introuvable'
                }), 404
        else:
            # Arrêter toutes les caméras
            for cam_id, ctx in camera_contexts.items():
                ctx.is_capturing = False
                try:
                    mqtt_service.publish_binary_sensor_state(f'capture_active_{cam_id}', False)
                except Exception:
                    pass
                    
            camera_service.stop_capture()  # Arrêter toutes
            camera_contexts.clear()
            
            return jsonify({
                'success': True,
                'message': 'Toutes les captures arrêtées'
            })
            
    except Exception as e:
        logger.exception("Erreur stop_capture")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    
    return jsonify({'status': 'Capture arrêtée'})

@app.route('/api/detections')
def get_detections():
    """Récupère la liste des détections configurées"""
    return jsonify(detection_service.get_detections())

@app.route('/api/detections', methods=['POST'])
def add_detection():
    """Ajoute une nouvelle détection personnalisée avec webhook optionnel"""
    data = request.json
    name = data.get('name')
    phrase = data.get('phrase')
    webhook_url = data.get('webhook_url')  # Optionnel
    
    if not name or not phrase:
        return jsonify({'error': 'Nom et phrase requis'}), 400
    
    # Valider l'URL du webhook si fournie
    if webhook_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(webhook_url)
            if not parsed.scheme in ['http', 'https']:
                return jsonify({'error': 'URL webhook invalide (doit utiliser http:// ou https://)'}), 400
            if not parsed.netloc:
                return jsonify({'error': 'URL webhook invalide (hostname manquant)'}), 400
            # Vérifier que ce n'est pas une URL locale dangereuse
            if parsed.hostname in ['localhost', '127.0.0.1', '::1'] or (parsed.hostname and parsed.hostname.startswith('192.168.')):
                logger.warning(f"URL webhook vers réseau local détectée: {webhook_url}")
        except Exception:
            return jsonify({'error': 'URL webhook malformée'}), 400
    
    detection_id = detection_service.add_detection(name, phrase, webhook_url)
    
    response_data = {'id': detection_id, 'status': 'Détection ajoutée'}
    if webhook_url:
        response_data['webhook_configured'] = True
        response_data['webhook_url'] = webhook_url
    
    return jsonify(response_data)

@app.route('/api/detections/<detection_id>', methods=['PUT', 'PATCH'])
def update_detection(detection_id):
    """Met à jour une détection personnalisée (nom, phrase, webhook)"""
    try:
        data = request.get_json() or {}
        name = data.get('name')
        phrase = data.get('phrase')
        webhook_url = data.get('webhook_url') if 'webhook_url' in data else None

        if not any([name, phrase]) and 'webhook_url' not in data:
            return jsonify({'error': 'Aucun champ à mettre à jour'}), 400

        updated = detection_service.update_detection(detection_id, name=name, phrase=phrase, webhook_url=webhook_url)
        if not updated:
            return jsonify({'error': 'Détection non trouvée'}), 404

        return jsonify({'success': True, 'detection': updated})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/detections/<detection_id>', methods=['DELETE'])
def delete_detection(detection_id):
    """Supprime une détection"""
    success = detection_service.remove_detection(detection_id)
    if success:
        return jsonify({'status': 'Détection supprimée'})
    else:
        return jsonify({'error': 'Détection non trouvée'}), 404

@app.route('/api/current_frame/<camera_id>')
def get_current_frame(camera_id):
    """Récupère l'image actuelle pour une caméra spécifique"""
    ctx = camera_contexts.get(camera_id)
    if not ctx or ctx.current_frame is None:
        return jsonify({'error': 'Aucune image disponible'}), 404
    
    # Encoder l'image en base64
    _, buffer = cv2.imencode('.jpg', ctx.current_frame)
    img_base64 = base64.b64encode(buffer).decode('utf-8')
    
    return jsonify({'image': f'data:image/jpeg;base64,{img_base64}'})

@app.route('/video_feed/<camera_id>')
def video_feed(camera_id):
    """Stream vidéo en temps réel pour une caméra spécifique"""
    if camera_id not in camera_contexts:
        return "Camera inconnue", 404

    ctx = camera_contexts[camera_id]
    
    def generate():
        global shutting_down
        error_count = 0
        max_errors = 5
        
        logger.info(f"[{camera_id}] Démarrage du flux vidéo...")
        
        try:
            while ctx.is_capturing:
                # Arrêt propre si shutdown demandé
                if shutting_down:
                    logger.info(f"[{camera_id}] Arrêt du flux vidéo - arrêt application en cours")
                    break
                try:
                    if ctx.current_frame is not None:
                        # Convertir l'image en JPEG avec compression optimisée
                        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]  # Qualité optimisée
                        success, buffer = cv2.imencode('.jpg', ctx.current_frame, encode_params)
                        if success:
                            frame = buffer.tobytes()
                            yield (b'--frame\r\n'
                                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                            error_count = 0  # Réinitialiser le compteur d'erreurs
                        else:
                            logger.error(f"[{camera_id}] Erreur d'encodage de l'image")
                            error_count += 1
                    else:
                        logger.debug(f"[{camera_id}] Pas d'image disponible")
                        error_count += 1
                        
                    # Si trop d'erreurs consécutives, arrêter le flux
                    if error_count > max_errors:
                        logger.error(f"[{camera_id}] Trop d'erreurs dans le flux vidéo, arrêt du flux")
                        break
                        
                    time.sleep(0.03)
                except Exception as e:
                    logger.exception(f"[{camera_id}] Exception dans le flux vidéo: {e}")
                    error_count += 1
                    if error_count > max_errors:
                        logger.error(f"[{camera_id}] Trop d'exceptions dans le flux vidéo, arrêt du flux")
                        break
                    time.sleep(0.5)  # Attendre un peu plus longtemps en cas d'erreur
        finally:
            logger.info(f"[{camera_id}] Flux vidéo fermé")
    
    # Retourner une réponse streaming MJPEG
    logger.info(f"[{camera_id}] Préparation de la réponse streaming MJPEG /video_feed")
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

def ha_polling_loop(ctx: CameraContext):
    """Boucle de capture via Home Assistant en utilisant HAService pour une caméra spécifique."""
    base_url = os.getenv('HA_BASE_URL', '').rstrip('/')
    token = os.getenv('HA_TOKEN', '')
    entity_id = os.getenv('HA_ENTITY_ID', '')
    image_attr = os.getenv('HA_IMAGE_ATTR', 'entity_picture')
    poll_interval = float(os.getenv('HA_POLL_INTERVAL', '1.0'))
    min_analysis_interval = float(os.getenv('MIN_ANALYSIS_INTERVAL', '0.1'))

    # Aligner les timeouts HA sur le timeout IA existant par simplicité
    try:
        ai_timeout = float(os.getenv('AI_TIMEOUT', '10'))
    except Exception:
        ai_timeout = 10.0

    service = HAService(
        base_url=base_url,
        token=token,
        entity_id=entity_id,
        image_attr=image_attr,
        poll_interval=poll_interval,
        state_timeout=ai_timeout,
        image_timeout=ai_timeout,
        logger=logging.getLogger(__name__)
    )

    def on_frame(frame):
        # Publier la frame courante
        ctx.current_frame = frame
        # Déclencher analyse si intervalle OK
        current_time = time.time()
        if not ctx.analysis_in_progress and (current_time - ctx.last_analysis_time) >= min_analysis_interval:
            ctx.analysis_in_progress = True
            threading.Thread(
                target=analyze_frame,
                args=(ctx, frame.copy(), current_time),
                daemon=True
            ).start()

    def is_running():
        return ctx.is_capturing

    service.run_loop(on_frame, is_running)


def capture_loop(ctx: CameraContext):
    """Boucle principale de capture RTSP"""
    min_analysis_interval = float(os.getenv('MIN_ANALYSIS_INTERVAL', '0.1'))
    
    while ctx.is_capturing:
        try:
            frame = camera_service.get_frame(ctx.camera_id)
            if frame is not None:
                ctx.current_frame = frame
                # Déclencher l'analyse si l'intervalle minimum est respecté
                current_time = time.time()
                if not ctx.analysis_in_progress and (current_time - ctx.last_analysis_time) >= min_analysis_interval:
                    ctx.analysis_in_progress = True
                    threading.Thread(
                        target=analyze_frame,
                        args=(ctx, frame.copy(), current_time),
                        daemon=True
                    ).start()

            # Cadence alignée sur la source si possible
            try:
                fps = camera_service.get_source_fps(ctx.camera_id) if hasattr(camera_service, 'get_source_fps') else None
                interval = 1.0 / fps if fps and fps > 0 else 0.02
            except Exception:
                interval = 0.02
            time.sleep(interval)

        except Exception as e:
            logger.exception(f"[{ctx.camera_id}] capture_loop error: {e}")
            time.sleep(0.1)


def analyze_frame(ctx: CameraContext, frame, start_time):
    """Analyse une image avec l'IA"""
    try:
        # Redimensionner l'image en 720p (1280x720) pour l'analyse
        resized_frame = resize_frame_for_analysis(frame)

        # Encoder l'image redimensionnée en base64
        _, buffer = cv2.imencode('.jpg', resized_frame)
        img_base64 = base64.b64encode(buffer).decode('utf-8')
        
        # Analyser avec les détections configurées
        result = detection_service.analyze_frame(img_base64, ctx.camera_id)

        # Détecter erreurs IA (timeouts et erreurs de connexion) et arrêter si nécessaire
        try:
            if isinstance(result, dict):
                err_text = (str(result.get('error', '')) + ' ' + str(result.get('details', ''))).lower()
                success_flag = bool(result.get('success', True))

                # Détection de timeout
                is_timeout = (not success_flag) and any(
                    kw in err_text for kw in ['timeout', 'timed out', 'read timed out', 'deadline exceeded']
                )
                # Détection d'erreurs de connexion/réseau
                is_connection_error = (not success_flag) and any(
                    kw in err_text for kw in [
                        'connection error', 'connection refused', 'failed to establish a new connection',
                        'connection reset', 'bad gateway', 'service unavailable', 'host unreachable',
                        'network is unreachable', 'cannot connect', 'name or service not known', 'dns']
                )

                if success_flag:
                    # Reset sur succès
                    if ctx.ai_consecutive_failures:
                        logger.debug(f"[{ctx.camera_id}] Réinitialisation du compteur d'échecs IA ({ctx.ai_consecutive_failures} → 0)")
                    ctx.ai_consecutive_failures = 0
                else:
                    ctx.ai_consecutive_failures += 1
                    logger.warning(f"[{ctx.camera_id}] Échec IA #{ctx.ai_consecutive_failures}: {err_text[:200]}")

                # Arrêt immédiat sur timeout ou erreur de connexion
                should_stop_now = is_timeout or is_connection_error
                # Arrêt après N échecs consécutifs (N=3)
                failure_threshold_reached = ctx.ai_consecutive_failures >= 3

                if ctx.is_capturing and (should_stop_now or failure_threshold_reached):
                    reason = 'timeout IA' if is_timeout else ('erreur de connexion IA' if is_connection_error else 'échecs IA répétés')
                    logger.error(f"[{ctx.camera_id}] 🛑 {reason} - arrêt de la capture")
                    ctx.is_capturing = False
                    try:
                        camera_service.stop_capture(ctx.camera_id)
                    except Exception as e_stop:
                        logger.warning(f"[{ctx.camera_id}] Erreur lors de l'arrêt de la capture après erreur IA: {e_stop}")
                    try:
                        mqtt_service.publish_binary_sensor_state(f'{ctx.camera_id}_capture_active', False)
                    except Exception:
                        pass
        except Exception:
            # Ne pas bloquer l'analyse si la détection d'erreur échoue
            pass
        
        # Calculer la durée de l'analyse
        end_time = time.time()
        ctx.last_analysis_duration = end_time - start_time
        # Calculer l'intervalle total (fin -> fin) par rapport à l'analyse précédente
        ctx.last_analysis_total_interval = end_time - ctx.last_analysis_time if ctx.last_analysis_time else 0
        ctx.last_analysis_time = end_time
        
        if ctx.last_analysis_total_interval and ctx.last_analysis_total_interval > 0:
            logger.info(f"[{ctx.camera_id}] Analyse terminée en {ctx.last_analysis_duration:.2f}s | Intervalle total: {ctx.last_analysis_total_interval:.2f}s | FPS total: {1.0/ctx.last_analysis_total_interval:.2f}")
        else:
            logger.info(f"[{ctx.camera_id}] Analyse terminée en {ctx.last_analysis_duration:.2f}s")
        
        # Publier les informations d'analyse via MQTT
        mqtt_service.publish_status({
            'camera_id': ctx.camera_id,
            'result': result,
            'duration': ctx.last_analysis_duration
        })
        
    except Exception as e:
        logger.error(f"[{ctx.camera_id}] analyse error: {e}")
        # Publier l'erreur via MQTT
        mqtt_service.publish_status({
            'camera_id': ctx.camera_id,
            'error': str(e),
            'duration': time.time() - start_time
        })
    finally:
        # Marquer l'analyse comme terminée, qu'elle ait réussi ou échoué
        ctx.analysis_in_progress = False

@app.route('/admin')
def admin():
    """Page d'administration"""
    return render_template('admin.html')

@app.route('/api/admin/config', methods=['GET'])
def get_admin_config():
    """Récupère la configuration actuelle"""
    try:
        config = {}
        
        # Lire le fichier .env
        env_path = '.env'
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        config[key] = value
        
        # Ajouter les paramètres par défaut s'ils n'existent pas
        defaults = {
            'AI_API_MODE': 'lmstudio',
            'AI_TIMEOUT': '10',
            'LOG_LEVEL': 'INFO',
            'OPENAI_MODEL': 'gpt-4-vision-preview',
            'LMSTUDIO_URL': 'http://127.0.0.1:11434/v1',
            'LMSTUDIO_MODEL': '',
            'OLLAMA_URL': 'http://127.0.0.1:11434/v1',
            'OLLAMA_MODEL': '',
            'MQTT_BROKER': '127.0.0.1',
            'MQTT_PORT': '1883',
            'MQTT_USERNAME': '',
            'MQTT_PASSWORD': '',
            'HA_DEVICE_NAME': 'IAction',
            'HA_DEVICE_ID': 'iaction_camera',
            'DEFAULT_RTSP_URL': 'rtsp://localhost:554/live',
            'RTSP_USERNAME': '',
            'RTSP_PASSWORD': '',
            'MIN_ANALYSIS_INTERVAL': '0.1',
            # Nouveau: capture mode & HA Polling
            'CAPTURE_MODE': 'rtsp',
            'HA_BASE_URL': '',
            'HA_TOKEN': '',
            'HA_ENTITY_ID': '',
            'HA_IMAGE_ATTR': 'entity_picture',
            'HA_POLL_INTERVAL': '1.0'
        }
        
        for key, default_value in defaults.items():
            if key not in config:
                config[key] = default_value
        
        return jsonify(config)
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la lecture de la configuration: {str(e)}'
        }), 500

@app.route('/api/admin/ai_test', methods=['GET'])
def admin_ai_test():
    """Teste la connexion au backend IA avec le modèle courant.
    Ne bloque pas le démarrage et retourne un JSON simple.
    """
    try:
        result = ai_service.test_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du test de connexion IA: {str(e)}'
        }), 200

@app.route('/api/admin/mqtt_test', methods=['GET'])
def admin_mqtt_test():
    """Retourne l'état de la connexion MQTT et tente une connexion rapide si déconnecté."""
    try:
        status = mqtt_service.get_connection_status() if hasattr(mqtt_service, 'get_connection_status') else {
            'connected': getattr(mqtt_service, 'is_connected', False),
            'broker': getattr(mqtt_service, 'broker', ''),
            'port': getattr(mqtt_service, 'port', 1883)
        }
        if not status.get('connected'):
            # Tentative de connexion rapide (non bloquante)
            try:
                mqtt_service.connect()
                t0 = time.time()
                while time.time() - t0 < 3:
                    if getattr(mqtt_service, 'is_connected', False):
                        break
                    time.sleep(0.2)
                status = mqtt_service.get_connection_status()
            except Exception:
                pass
        return jsonify({ 'success': True, 'status': status })
    except Exception as e:
        return jsonify({ 'success': False, 'error': str(e) }), 200

@app.route('/api/admin/rtsp_test', methods=['POST'])
def admin_rtsp_test():
    """Teste une URL RTSP (dans le body JSON: { url }) ou la valeur DEFAULT_RTSP_URL si absente."""
    try:
        test_url = None
        try:
            data = request.get_json(silent=True) or {}
            test_url = data.get('url')
        except Exception:
            test_url = None
        if not test_url:
            test_url = os.getenv('DEFAULT_RTSP_URL', '')
        status = camera_service._test_rtsp_connection(test_url) if hasattr(camera_service, '_test_rtsp_connection') else 'unsupported'
        return jsonify({
            'success': True,
            'url': test_url,
            'status': status
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 200

@app.route('/api/admin/reload', methods=['POST'])
def admin_hot_reload():
    """Recharge la configuration (.env) et reconfigure les services sans redémarrer."""
    try:
        # Recharger .env
        try:
            load_dotenv(override=True)
        except Exception:
            pass

        status = {}

        # Mettre à jour le niveau de logs dynamiquement
        try:
            lvl_name = os.getenv('LOG_LEVEL', 'INFO').upper()
            new_level = getattr(logging, lvl_name, logging.INFO)
            logging.getLogger().setLevel(new_level)  # root
            logger.setLevel(new_level)
            status['log_level'] = lvl_name
        except Exception as e:
            status['log_level_error'] = str(e)

        # Recharger AI
        try:
            if hasattr(ai_service, 'reload_from_env'):
                status['ai_reloaded'] = bool(ai_service.reload_from_env())
            else:
                status['ai_reloaded'] = False
        except Exception as e:
            status['ai_error'] = str(e)

        # Recharger MQTT et reconfigurer les capteurs
        try:
            if hasattr(mqtt_service, 'reload_from_env'):
                status['mqtt_reloaded'] = bool(mqtt_service.reload_from_env())
            else:
                status['mqtt_reloaded'] = False
        except Exception as e:
            status['mqtt_error'] = str(e)

        try:
            if getattr(mqtt_service, 'is_connected', False):
                if hasattr(detection_service, 'reconfigure_mqtt_sensors'):
                    detection_service.reconfigure_mqtt_sensors()
                    status['mqtt_sensors_reconfigured'] = True
        except Exception as e:
            status['mqtt_sensors_error'] = str(e)

        # Recharger caméra (cache/cfg)
        try:
            if hasattr(camera_service, 'refresh_from_env'):
                camera_service.refresh_from_env()
                status['camera_refreshed'] = True
        except Exception as e:
            status['camera_error'] = str(e)

        # Mettre à jour l'intervalle d'analyse
        try:
            detection_service.min_analysis_interval = float(os.getenv('MIN_ANALYSIS_INTERVAL', '0.1'))
            status['min_analysis_interval'] = detection_service.min_analysis_interval
        except Exception as e:
            status['min_analysis_interval_error'] = str(e)

        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/config', methods=['POST'])
def save_admin_config():
    """Sauvegarde la configuration"""
    try:
        config = request.get_json()
        
        if not config:
            return jsonify({
                'success': False,
                'error': 'Aucune configuration fournie'
            }), 400
        
        # Construire le contenu du fichier .env
        env_content = []
        
        # Configuration IA
        env_content.append("# Configuration IA")
        env_content.append(f"AI_API_MODE={_sanitize_env_value(config.get('AI_API_MODE', 'lmstudio'), 'AI_API_MODE')}")
        env_content.append(f"AI_TIMEOUT={_sanitize_env_value(config.get('AI_TIMEOUT', '10'), 'AI_TIMEOUT')}")
        env_content.append("")

        # Configuration Logs
        env_content.append("# Configuration Logs")
        env_content.append(f"LOG_LEVEL={_sanitize_env_value(config.get('LOG_LEVEL', 'INFO'), 'LOG_LEVEL')}")
        env_content.append("")
        
        # Configuration OpenAI
        env_content.append("# Configuration OpenAI")
        env_content.append(f"OPENAI_API_KEY={_sanitize_env_value(config.get('OPENAI_API_KEY', ''), 'OPENAI_API_KEY')}")
        env_content.append(f"OPENAI_MODEL={_sanitize_env_value(config.get('OPENAI_MODEL', 'gpt-4-vision-preview'), 'OPENAI_MODEL')}")
        env_content.append("")
        
        # Configuration LM Studio
        env_content.append("# Configuration LM Studio")
        env_content.append(f"LMSTUDIO_URL={_sanitize_env_value(config.get('LMSTUDIO_URL', 'http://127.0.0.1:11434/v1'), 'LMSTUDIO_URL')}")
        env_content.append(f"LMSTUDIO_MODEL={_sanitize_env_value(config.get('LMSTUDIO_MODEL', ''), 'LMSTUDIO_MODEL')}")
        env_content.append("")
        
        # Configuration Ollama
        env_content.append("# Configuration Ollama")
        env_content.append(f"OLLAMA_URL={_sanitize_env_value(config.get('OLLAMA_URL', 'http://127.0.0.1:11434/v1'), 'OLLAMA_URL')}")
        env_content.append(f"OLLAMA_MODEL={_sanitize_env_value(config.get('OLLAMA_MODEL', ''), 'OLLAMA_MODEL')}")
        env_content.append("")
        
        # Configuration MQTT
        env_content.append("# Configuration MQTT")
        env_content.append(f"MQTT_BROKER={_sanitize_env_value(config.get('MQTT_BROKER', '127.0.0.1'), 'MQTT_BROKER')}")
        env_content.append(f"MQTT_PORT={_sanitize_env_value(config.get('MQTT_PORT', '1883'), 'MQTT_PORT')}")
        env_content.append(f"MQTT_USERNAME={_sanitize_env_value(config.get('MQTT_USERNAME', ''), 'MQTT_USERNAME')}")
        env_content.append(f"MQTT_PASSWORD={_sanitize_env_value(config.get('MQTT_PASSWORD', ''), 'MQTT_PASSWORD')}")
        env_content.append("")
        
        # Configuration Home Assistant
        env_content.append("\n# Configuration Home Assistant")
        env_content.append(f"HA_DEVICE_NAME={_sanitize_env_value(config.get('HA_DEVICE_NAME', 'IAction'), 'HA_DEVICE_NAME')}")
        env_content.append(f"HA_DEVICE_ID={_sanitize_env_value(config.get('HA_DEVICE_ID', 'iaction_camera'), 'HA_DEVICE_ID')}")
        env_content.append("")
        
        # Configuration Caméra
        env_content.append("\n# Configuration Caméra")
        env_content.append(f"CAPTURE_MODE={_sanitize_env_value(config.get('CAPTURE_MODE', 'rtsp'), 'CAPTURE_MODE')}")
        env_content.append(f"DEFAULT_RTSP_URL={_sanitize_env_value(config.get('DEFAULT_RTSP_URL', ''), 'DEFAULT_RTSP_URL')}")
        env_content.append(f"RTSP_USERNAME={_sanitize_env_value(config.get('RTSP_USERNAME', ''), 'RTSP_USERNAME')}")
        env_content.append(f"RTSP_PASSWORD={_sanitize_env_value(config.get('RTSP_PASSWORD', ''), 'RTSP_PASSWORD')}")

        # Configuration HA Polling
        env_content.append("\n# Configuration HA Polling")
        env_content.append(f"HA_BASE_URL={_sanitize_env_value(config.get('HA_BASE_URL', ''), 'HA_BASE_URL')}")
        env_content.append(f"HA_TOKEN={_sanitize_env_value(config.get('HA_TOKEN', ''), 'HA_TOKEN')}")
        env_content.append(f"HA_ENTITY_ID={_sanitize_env_value(config.get('HA_ENTITY_ID', ''), 'HA_ENTITY_ID')}")
        env_content.append(f"HA_IMAGE_ATTR={_sanitize_env_value(config.get('HA_IMAGE_ATTR', 'entity_picture'), 'HA_IMAGE_ATTR')}")
        env_content.append(f"HA_POLL_INTERVAL={_sanitize_env_value(config.get('HA_POLL_INTERVAL', '1.0'), 'HA_POLL_INTERVAL')}")

        # Configuration Analyse
        env_content.append("\n# Configuration Analyse")
        env_content.append(f"MIN_ANALYSIS_INTERVAL={_sanitize_env_value(config.get('MIN_ANALYSIS_INTERVAL', '0.1'), 'MIN_ANALYSIS_INTERVAL')}")

        # Écrire le fichier .env
        with open('.env', 'w', encoding='utf-8') as f:
            f.write('\n'.join(env_content))
        
        return jsonify({
            'success': True,
            'message': 'Configuration sauvegardée avec succès'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la sauvegarde: {str(e)}'
        }), 500

@app.route('/api/admin/cameras', methods=['GET'])
def get_cameras_status():
    """Récupère le statut de toutes les caméras configurées"""
    try:
        cameras_status = {}
        for camera_id, ctx in camera_contexts.items():
            cameras_status[camera_id] = {
                'id': camera_id,
                'is_capturing': ctx.is_capturing,
                'last_analysis_time': ctx.last_analysis_time,
                'last_analysis_duration': ctx.last_analysis_duration,
                'analysis_in_progress': ctx.analysis_in_progress
            }
        
        return jsonify({
            'success': True,
            'cameras': cameras_status,
            'total_cameras': len(camera_contexts),
            'active_cameras': len([ctx for ctx in camera_contexts.values() if ctx.is_capturing])
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/admin/cameras/test_multiple', methods=['POST'])
def test_multiple_cameras():
    """Teste la connexion de plusieurs caméras sans les démarrer"""
    try:
        data = request.json or {}
        cameras_config = data.get('cameras', [])
        
        if not cameras_config:
            return jsonify({
                'success': False,
                'error': 'Aucune caméra à tester'
            }), 400
        
        results = []
        
        for camera_config in cameras_config:
            camera_id = camera_config.get('id', 'unknown')
            camera_name = camera_config.get('name', f'Caméra {camera_id}')
            mode = camera_config.get('mode', 'rtsp')
            
            try:
                if mode == 'rtsp':
                    rtsp_url = camera_config.get('rtsp_url')
                    if not rtsp_url:
                        results.append({
                            'camera_id': camera_id,
                            'camera_name': camera_name,
                            'success': False,
                            'status': 'not_configured',
                            'message': 'URL RTSP manquante'
                        })
                        continue
                    
                    # Test de connexion RTSP (sans démarrer l'analyse)
                    if hasattr(camera_service, '_test_rtsp_connection'):
                        test_status = camera_service._test_rtsp_connection(rtsp_url)
                    else:
                        test_status = 'unsupported'
                    
                    results.append({
                        'camera_id': camera_id,
                        'camera_name': camera_name,
                        'success': test_status == 'online',
                        'status': test_status,
                        'message': f'Test RTSP: {test_status}',
                        'url': rtsp_url
                    })
                    
                elif mode == 'ha_polling':
                    ha_entity = camera_config.get('ha_entity')
                    if not ha_entity:
                        results.append({
                            'camera_id': camera_id,
                            'camera_name': camera_name,
                            'success': False,
                            'status': 'not_configured',
                            'message': 'Entité Home Assistant manquante'
                        })
                        continue
                    
                    # Test basique de configuration HA
                    ha_url = os.getenv('HA_URL', '')
                    ha_token = os.getenv('HA_TOKEN', '')
                    
                    if not ha_url or not ha_token:
                        results.append({
                            'camera_id': camera_id,
                            'camera_name': camera_name,
                            'success': False,
                            'status': 'not_configured',
                            'message': 'Configuration Home Assistant incomplète'
                        })
                        continue
                    
                    results.append({
                        'camera_id': camera_id,
                        'camera_name': camera_name,
                        'success': True,
                        'status': 'configured',
                        'message': f'Configuration HA OK: {ha_entity}'
                    })
                    
                else:
                    results.append({
                        'camera_id': camera_id,
                        'camera_name': camera_name,
                        'success': False,
                        'status': 'error',
                        'message': f'Mode non supporté: {mode}'
                    })
                    
            except Exception as e:
                results.append({
                    'camera_id': camera_id,
                    'camera_name': camera_name,
                    'success': False,
                    'status': 'error',
                    'message': f'Erreur test: {str(e)}'
                })
        
        success_count = sum(1 for r in results if r['success'])
        total_count = len(results)
        
        return jsonify({
            'success': True,
            'results': results,
            'summary': {
                'total': total_count,
                'success': success_count,
                'failed': total_count - success_count
            }
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/admin/cameras/start_multiple', methods=['POST'])
def start_multiple_cameras():
    """Démarre plusieurs caméras depuis la configuration admin"""
    try:
        data = request.json or {}
        cameras_config = data.get('cameras', [])
        
        if not cameras_config:
            return jsonify({
                'success': False,
                'error': 'Aucune caméra à démarrer'
            }), 400
        
        results = []
        
        for camera_config in cameras_config:
            camera_id = camera_config.get('id', 'unknown')
            try:
                # Créer le contexte caméra si inexistant
                if camera_id not in camera_contexts:
                    camera_contexts[camera_id] = CameraContext(camera_id)
                
                ctx = camera_contexts[camera_id]
                
                if ctx.is_capturing:
                    results.append({
                        'camera_id': camera_id,
                        'success': False,
                        'message': f'Caméra {camera_id} déjà en cours'
                    })
                    continue
                
                mode = camera_config.get('mode', 'rtsp')
                
                if mode == 'rtsp':
                    rtsp_url = camera_config.get('rtsp_url')
                    if not rtsp_url:
                        results.append({
                            'camera_id': camera_id,
                            'success': False,
                            'message': 'URL RTSP manquante'
                        })
                        continue
                    
                    success = camera_service.start_capture(camera_id, rtsp_url, 'rtsp')
                    if not success:
                        results.append({
                            'camera_id': camera_id,
                            'success': False,
                            'message': 'Échec démarrage RTSP'
                        })
                        continue
                    
                    ctx.is_capturing = True
                    threading.Thread(target=capture_loop, args=(ctx,), daemon=True).start()
                    
                    try:
                        mqtt_service.publish_binary_sensor_state(f"capture_active_{camera_id}", True)
                    except Exception:
                        pass
                    
                    results.append({
                        'camera_id': camera_id,
                        'success': True,
                        'message': f'RTSP démarré pour {camera_id}'
                    })
                    
                elif mode == 'ha_polling':
                    # Configuration HA polling pour cette caméra spécifique
                    # TODO: Implémenter le support HA polling multi-caméra
                    results.append({
                        'camera_id': camera_id,
                        'success': False,
                        'message': 'HA Polling multi-caméra pas encore implémenté'
                    })
                
            except Exception as e:
                results.append({
                    'camera_id': camera_id,
                    'success': False,
                    'message': f'Erreur: {str(e)}'
                })
        
        success_count = len([r for r in results if r['success']])
        
        return jsonify({
            'success': True,
            'message': f'{success_count}/{len(results)} caméras démarrées',
            'results': results
        })
        
    except Exception as e:
        logger.exception("Erreur start_multiple_cameras")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/admin/restart', methods=['POST'])
def restart_app():
    """Redémarre l'application"""
    try:
        # Récupérer la fonction shutdown du serveur pour libérer le port proprement
        shutdown_fn = None
        try:
            shutdown_fn = request.environ.get('werkzeug.server.shutdown')
        except Exception:
            shutdown_fn = None
        # Démarrer un redémarrage différé pour que la réponse HTTP parte correctement
        threading.Thread(target=_delayed_self_restart, kwargs={'delay_sec': 1.0, 'shutdown_fn': shutdown_fn}, daemon=True).start()
        return jsonify({'success': True, 'message': 'Redémarrage en cours (nouveau processus).'} )
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du redémarrage: {str(e)}'
        }), 500

@app.route('/api/admin/shutdown', methods=['POST'])
def shutdown_app():
    """Arrête proprement l'application (fallback si Ctrl+C ne fonctionne pas).
    Limité à l'accès local (127.0.0.1 / ::1).
    """
    try:
        ra = request.remote_addr
        if ra not in ('127.0.0.1', '::1'):
            return jsonify({'success': False, 'error': 'Accès refusé'}), 403
        logger.info("Demande d'arrêt via /api/admin/shutdown")
        cleanup()
        # Sortie différée pour laisser la réponse HTTP partir
        def _exit():
            time.sleep(0.2)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return jsonify({'success': True, 'message': 'Arrêt en cours...'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Fonction pour nettoyer les ressources avant l'arrêt de l'application
def cleanup():
    global shutting_down
    if shutting_down:
        return
    logger.info("Nettoyage des ressources...")
    # Poser le flag d'arrêt
    shutting_down = True
    
    # Arrêter toutes les caméras
    for camera_id, ctx in camera_contexts.items():
        ctx.is_capturing = False
        try:
            # Publier l'état de capture (OFF) avant de se déconnecter
            mqtt_service.publish_binary_sensor_state(f'capture_active_{camera_id}', False)
        except Exception:
            pass
    
    try:
        camera_service.stop_capture()  # Arrêter toutes les captures
    except Exception as e:
        logger.warning(f"Erreur lors de l'arrêt des caméras: {e}")
    
    try:
        mqtt_service.disconnect()
    except Exception as e:
        logger.warning(f"Erreur lors de la déconnexion MQTT: {e}")
        
    # Vider le registre des caméras
    camera_contexts.clear()

# Enregistrer la fonction de nettoyage pour qu'elle soit appelée à la fermeture
import atexit
atexit.register(cleanup)

if __name__ == '__main__':
    # Gestion des signaux (Ctrl+C / arrêt système)
    try:
        import signal
        def _handle_signal(signum, frame):
            logger.info(f"Signal reçu ({signum}), arrêt en cours...")
            try:
                cleanup()
            finally:
                # Petite attente pour laisser finir les réponses HTTP
                time.sleep(0.2)
                os._exit(0)
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, _handle_signal)
        # Sous Windows, gérer aussi Ctrl+Pause (SIGBREAK)
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, _handle_signal)
    except Exception:
        # En cas d'échec, on poursuivra sans handler explicite
        pass
    logger.info("=== DÉMARRAGE IACTION ===")
    # Si ce processus est lancé par un redémarrage, attendre la libération du port HTTP
    try:
        if os.environ.get('IACTION_WAIT_FOR_PID'):
            logger.info("⏳ Attente de la libération du port 5002 par l'ancien processus (bind test)...")
            _wait_until_bind_possible('0.0.0.0', 5002, timeout=10.0)
        os.environ.pop('IACTION_WAIT_FOR_PID', None)
    except Exception:
        pass
    logger.info("Tentative de connexion au broker MQTT...")
    
    # Initier la connexion MQTT
    mqtt_service.connect()
    
    # Attendre que la connexion soit établie (ou échoue)
    logger.info("Vérification de la connexion MQTT...")
    max_wait = 10  # Attendre maximum 10 secondes
    wait_time = 0
    
    while wait_time < max_wait:
        if mqtt_service.is_connected:
            logger.info("✅ MQTT: Connexion réussie au broker")
            logger.info("✅ MQTT: Capteurs configurés pour Home Assistant")
            # Reconfigurer les capteurs des détections après connexion MQTT
            try:
                if hasattr(detection_service, 'reconfigure_mqtt_sensors'):
                    detection_service.reconfigure_mqtt_sensors()
            except Exception as e:
                logger.error(f"Erreur reconfiguration MQTT des détections: {e}")
            break
        time.sleep(1)
        wait_time += 1
        if wait_time % 3 == 0:
            logger.info(f"⏳ MQTT: Tentative de connexion... ({wait_time}/{max_wait}s)")
    
    if not mqtt_service.is_connected:
        logger.error("❌ MQTT: Connexion échouée - Les capteurs ne seront pas disponibles")
        logger.error("   Vérifiez votre broker MQTT et votre configuration .env")
    
    logger.info("\n=== DÉMARRAGE DU SERVEUR WEB ===")
    debug_mode = '--debug' in sys.argv
    no_reloader = '--no-reloader' in sys.argv or os.getenv('NO_RELOADER', '').lower() in ('1', 'true', 'yes')
    is_windows = os.name == 'nt'

    # Désactiver systématiquement le reloader pour éviter WinError 10038 (Windows)
    os.environ.pop('WERKZEUG_RUN_MAIN', None)
    os.environ.pop('WERKZEUG_SERVER_FD', None)

    try:
        if debug_mode:
            logger.info("Mode: DEBUG")
            _run_web_server_with_retry(host='0.0.0.0', port=5002, debug=True)
        else:
            logger.info("Mode: PRODUCTION")
            _run_web_server_with_retry(host='0.0.0.0', port=5002, debug=False)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt reçu, arrêt en cours...")
        cleanup()
        os._exit(0)
