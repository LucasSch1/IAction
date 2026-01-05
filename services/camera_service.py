import cv2
import threading
import time
import os
import hashlib
import numpy as np
from PIL import Image
import io
from dotenv import load_dotenv
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

class CameraService:
    def __init__(self):
        # Support multi-caméra - dictionnaire des captures par camera_id
        self.captures = {}  # camera_id -> {'cap': VideoCapture, 'info': dict}
        self.lock = threading.Lock()
        self.cameras_cache = None
        self.cache_time = 0
        self.cache_duration = 30  # Cache pendant 30 secondes
        
        # Optimisations IA
        self.motion_threshold = float(os.getenv('MOTION_THRESHOLD', '5.0'))  # Seuil de détection de mouvement %
        self.ai_image_quality = int(os.getenv('AI_IMAGE_QUALITY', '60'))    # Qualité JPEG pour l'IA (60%)
        self.ai_max_width = int(os.getenv('AI_MAX_WIDTH', '1280'))          # Largeur max pour l'IA
        self.ai_max_height = int(os.getenv('AI_MAX_HEIGHT', '720'))         # Hauteur max pour l'IA
        self.frame_cache = {}  # camera_id -> {'last_hash': str, 'last_analysis_time': float}
        self.motion_detection_enabled = os.getenv('MOTION_DETECTION', 'true').lower() == 'true'
        
        load_dotenv()
        
        # Configuration RTSP - Support jusqu'à 6 caméras
        self.default_rtsp_urls = []
        for i in range(6):  # Support de 6 caméras maximum
            url_key = f'DEFAULT_RTSP_URL_{i+1}' if i > 0 else 'DEFAULT_RTSP_URL'
            username_key = f'RTSP_USERNAME_{i+1}' if i > 0 else 'RTSP_USERNAME' 
            password_key = f'RTSP_PASSWORD_{i+1}' if i > 0 else 'RTSP_PASSWORD'
            name_key = f'RTSP_NAME_{i+1}' if i > 0 else 'RTSP_NAME'
            width_key = f'RTSP_WIDTH_{i+1}' if i > 0 else 'RTSP_WIDTH'
            height_key = f'RTSP_HEIGHT_{i+1}' if i > 0 else 'RTSP_HEIGHT'
            fps_key = f'RTSP_FPS_{i+1}' if i > 0 else 'RTSP_FPS'
            
            url = os.getenv(url_key, '')
            if url:  # N'ajouter que si l'URL est définie
                self.default_rtsp_urls.append({
                    'name': os.getenv(name_key, f'RTSP Camera {i+1}'),
                    'url': url,
                    'username': os.getenv(username_key, ''),
                    'password': os.getenv(password_key, ''),
                    'width': int(os.getenv(width_key, '640')),  # Résolution réduite par défaut
                    'height': int(os.getenv(height_key, '480')),
                    'fps': int(os.getenv(fps_key, '15')),  # FPS réduit par défaut
                    'enabled': True
                })
        

    
    def get_available_cameras(self):
        """Récupère les caméras RTSP disponibles"""
        if self.cameras_cache is not None and time.time() - self.cache_time < self.cache_duration:
            return self.cameras_cache
        
        logger.info("=== Chargement des options RTSP ===")
        
        # Seules les caméras RTSP sont supportées
        cameras = self._get_rtsp_cameras()
        
        logger.info(f"Options disponibles: {len(cameras)} source(s) RTSP configurée(s)")
        for cam in cameras:
            logger.info(f" - {cam['name']} (type: {cam['type']}, id: {cam['id']})")
        logger.info("=== Fin du chargement des options ===")
        
        # Mettre en cache le résultat
        self.cameras_cache = cameras
        self.cache_time = time.time()
        
        return cameras
    

    
    def _get_rtsp_cameras(self):
        """Récupère les caméras RTSP configurées"""
        rtsp_cameras = []
        
        # Ajouter les URLs RTSP par défaut
        for idx, rtsp_config in enumerate(self.default_rtsp_urls):
            if rtsp_config['enabled']:
                camera_name = f"RTSP Camera {idx + 1}"
                if rtsp_config['name']:
                    camera_name = rtsp_config['name']
                
                rtsp_cameras.append({
                    'id': f'rtsp_{idx}',
                    'name': camera_name,
                    'type': 'rtsp',
                    'url': rtsp_config['url'],
                    'username': rtsp_config['username'],
                    'password': rtsp_config['password'],
                    'test_status': self._test_rtsp_connection(rtsp_config['url'])
                })
        
        # Ajouter l'option RTSP personnalisée
        rtsp_cameras.append({
            'id': 'rtsp_custom',
            'name': '📹 Caméra IP - URL personnalisée',
            'type': 'rtsp',
            'description': 'Saisissez votre propre URL RTSP'
        })
        
        return rtsp_cameras
    
    def _test_rtsp_connection(self, url, timeout=3):
        """Test la connexion RTSP avec timeout réduit pour tests multiples"""
        if not url:
            return 'not_configured'
        
        try:
            # Timeout plus court pour éviter les blocages lors de tests multiples
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Définir un timeout court
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout * 1000)
            
            if cap.isOpened():
                # Test de lecture rapide
                ret, frame = cap.read()
                cap.release()
                return 'online' if ret and frame is not None else 'error'
            else:
                cap.release()
                return 'offline'
        except Exception:
            return 'error'
    
    def get_camera_info(self, camera_id):
        """Obtient des informations détaillées sur une caméra"""
        cameras = self.get_available_cameras()
        for camera in cameras:
            if str(camera['id']) == str(camera_id):
                return camera
        return None
    
    def validate_rtsp_url(self, url):
        """Valide et normalise une URL RTSP"""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ['rtsp', 'http', 'https']:
                return False, "Protocol non supporté. Utilisez rtsp://, http:// ou https://"
            
            if not parsed.hostname:
                return False, "Hostname manquant dans l'URL"
            
            return True, "URL valide"
        except Exception as e:
            return False, f"URL invalide: {str(e)}"
    
    def build_rtsp_url(self, ip, port=554, username='', password='', path=''):
        """Construit une URL RTSP à partir des composants"""
        if username and password:
            auth = f"{username}:{password}@"
        else:
            auth = ""
        
        if not path.startswith('/'):
            path = '/' + path if path else '/'
        
        return f"rtsp://{auth}{ip}:{port}{path}"
    
    def start_capture(self, camera_id, source, source_type=None, rtsp_url=None):
        """Démarre la capture RTSP pour une caméra spécifique"""
        with self.lock:
            # Arrêter la capture existante pour cette caméra
            if camera_id in self.captures:
                self.stop_capture(camera_id)
            
            try:
                # Seul RTSP est supporté
                source_type = 'rtsp'
                logger.info(f"[{camera_id}] Démarrage de la capture RTSP - Source: {source}")
                
                if source_type == 'rtsp':
                    # Caméra RTSP
                    actual_url = rtsp_url if rtsp_url else source
                    
                    # Gestion des caméras RTSP préconfigurées
                    if isinstance(source, str) and source.startswith('rtsp_'):
                        camera_info = self.get_camera_info(source)
                        if camera_info and 'url' in camera_info:
                            actual_url = camera_info['url']
                            if camera_info.get('username') and camera_info.get('password'):
                                # Construire l'URL avec authentification
                                from urllib.parse import urlparse, urlunparse
                                parsed = urlparse(actual_url)
                                auth_netloc = f"{camera_info['username']}:{camera_info['password']}@{parsed.hostname}"
                                if parsed.port:
                                    auth_netloc += f":{parsed.port}"
                                parsed = parsed._replace(netloc=auth_netloc)
                                actual_url = urlunparse(parsed)
                    
                    logger.info(f"[{camera_id}] Ouverture du flux RTSP: {actual_url[:50]}...")
                    
                    # Configuration optimisée pour RTSP (FFMPEG)
                    cap = cv2.VideoCapture(actual_url, cv2.CAP_FFMPEG)
                    
                    # Configuration RTSP spécifique pour latence minimale et performance
                    if cap.isOpened():
                        # Buffer minimal pour latence
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        
                        # Optimisations de performance par caméra
                        camera_info = self.get_camera_info(source) if isinstance(source, str) and source.startswith('rtsp_') else None
                        if camera_info:
                            # Appliquer résolution personnalisée si configurée
                            if 'width' in camera_info and 'height' in camera_info:
                                cap.set(cv2.CAP_PROP_FRAME_WIDTH, camera_info['width'])
                                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, camera_info['height'])
                            # Appliquer FPS personnalisé
                            if 'fps' in camera_info:
                                cap.set(cv2.CAP_PROP_FPS, camera_info['fps'])
                        else:
                            # Valeurs par défaut pour caméras personnalisées (optimisées pour performance)
                            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                            cap.set(cv2.CAP_PROP_FPS, 15)
                        
                        # Optimisations RTSP supplémentaires
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
                        # Timeout pour éviter les blocages
                        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
                        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
                        
                        # Stocker les informations de la caméra
                        self.captures[camera_id] = {
                            'cap': cap,
                            'source': source,
                            'type': source_type,
                            'url': actual_url,
                            'last_frame_ts': 0.0,
                            'reconnect_attempts': 0,
                            'next_reconnect_time': 0.0,
                            'last_frame': None,  # Pour détection de mouvement
                            'motion_detected': True,  # Force première analyse
                            'frame_count': 0,
                            'last_motion_time': 0.0
                        }
                        
                        logger.info(f"[{camera_id}] Capture RTSP démarrée avec succès")
                        return True
                    else:
                        logger.error(f"[{camera_id}] Impossible d'ouvrir la source vidéo RTSP")
                        cap.release()
                        return False
                
            except Exception as e:
                logger.exception(f"[{camera_id}] Erreur lors du démarrage de la capture: {e}")
                return False
                logger.info("Configuration des propriétés de la caméra RTSP")
                
                # Utiliser la résolution native de la source (ne pas forcer W/H)
                # Conserver un buffer minimal pour réduire la latence
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                
                # Test de lecture avec plusieurs tentatives pour RTSP
                max_attempts = 3
                test_frame = None
                
                for attempt in range(max_attempts):
                    ret, test_frame = self.cap.read()
                    if ret and test_frame is not None and test_frame.size > 0:
                        break
                    if attempt < max_attempts - 1:
                        logger.warning(f"Tentative {attempt + 1} échouée, nouvelle tentative...")
                        time.sleep(0.5)
                
                if test_frame is None or test_frame.size == 0:
                    logger.error("Impossible de lire une image depuis la caméra")
                    self.cap.release()
                    self.cap = None
                    self.current_url = None
                    return False
                    
                logger.info(f"Capture démarrée avec succès - Dimensions: {test_frame.shape}")
                
                self.current_source = rtsp_url if rtsp_url else source
                self.current_type = source_type
                self.is_capturing = True
                self.last_frame_ts = time.time()
                self.reconnect_attempts = 0
                self.next_reconnect_time = 0.0
                
                return True
                
            except Exception as e:
                logger.error(f"Erreur lors du démarrage de la capture: {e}")
                if self.cap:
                    self.cap.release()
                    self.cap = None
                self.current_url = None
                return False
    
    def stop_capture(self, camera_id=None):
        """Arrête la capture pour une caméra spécifique ou toutes les caméras"""
        with self.lock:
            if camera_id:
                # Arrêter une caméra spécifique
                if camera_id in self.captures:
                    logger.info(f"[{camera_id}] Arrêt de la capture RTSP")
                    camera_info = self.captures[camera_id]
                    if camera_info['cap']:
                        camera_info['cap'].release()
                    del self.captures[camera_id]
            else:
                # Arrêter toutes les caméras
                logger.info("Arrêt de toutes les captures RTSP")
                for cam_id, camera_info in self.captures.items():
                    if camera_info['cap']:
                        camera_info['cap'].release()
                self.captures.clear()
    
    def get_frame(self, camera_id):
        """Récupère une image de la caméra spécifique avec gestion améliorée"""
        with self.lock:
            if camera_id not in self.captures:
                return None
            
            camera_info = self.captures[camera_id]
            cap = camera_info['cap']
            
            if not cap:
                now = time.time()
                if now >= camera_info['next_reconnect_time']:
                    logger.warning(f"[{camera_id}] Capteur RTSP absent, tentative de reconnexion...")
                    return self._reconnect_camera(camera_id)
                return None
                
            try:
                # Si le flux est fermé, tenter une reconnexion (respecter la fenêtre)
                if not cap.isOpened():
                    now = time.time()
                    if now >= camera_info['next_reconnect_time']:
                        logger.warning(f"[{camera_id}] Capteur RTSP fermé, tentative de reconnexion immédiate...")
                        return self._reconnect_camera(camera_id)
                    return None

                # Watchdog: si aucune frame fraîche depuis trop longtemps, forcer une reconnexion
                stale_threshold = float(os.getenv('RTSP_STALE_THRESHOLD', '3.0'))
                if camera_info['last_frame_ts'] and stale_threshold > 0 and (time.time() - camera_info['last_frame_ts']) > stale_threshold:
                    now = time.time()
                    if now >= camera_info['next_reconnect_time']:
                        logger.warning(f"[{camera_id}] Aucune frame récente depuis {time.time() - camera_info['last_frame_ts']:.1f}s, tentative de reconnexion...")
                        return self._reconnect_camera(camera_id)

                # Pour RTSP, lire la frame la plus récente (skip des frames en buffer)
                ret = False
                frame = None
                # Lire plusieurs frames pour vider le buffer et obtenir la plus récente
                skip_frames = 2  # Réduire le nombre de frames à skip pour économiser CPU
                for _ in range(skip_frames):
                    ret, frame = cap.read()
                    if not ret:
                        break

                if ret and frame is not None and frame.size > 0:
                    camera_info['last_frame_ts'] = time.time()
                    # reset compteur de reconnexion sur succès
                    camera_info['reconnect_attempts'] = 0
                    
                    # Mettre à jour les statistiques de frame
                    camera_info['frame_count'] = camera_info.get('frame_count', 0) + 1
                    
                    return frame
                else:
                    # Plusieurs tentatives avec délai
                    logger.warning(f"[{camera_id}] Échec de lecture de frame")
                    return None
                    
            except Exception as e:
                logger.exception(f"[{camera_id}] Erreur lors de la lecture: {e}")
                return None
    
    def _reconnect_camera(self, camera_id):
        """Tente de reconnecter la caméra avec backoff exponentiel et URL exacte"""
        if camera_id not in self.captures:
            return None
            
        camera_info = self.captures[camera_id]
        
        try:
            # Fermer la connexion actuelle
            if camera_info['cap']:
                try:
                    camera_info['cap'].release()
                except Exception:
                    pass
            
            max_tries = 3
            last_err = None
            for i in range(max_tries):
                logger.info(f"[{camera_id}] 🔄 Reconnexion RTSP (tentative {i+1}/{max_tries}) vers {str(camera_info['url'])[:50]}...")
                cap = cv2.VideoCapture(camera_info['url'], cv2.CAP_FFMPEG)
                if cap and cap.isOpened():
                    # Configurer: latence minimale sans forcer la résolution
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
                    # Lire une image
                    ret, frame = cap.read()
                    if ret and frame is not None and frame.size > 0:
                        camera_info['cap'] = cap
                        camera_info['last_frame_ts'] = time.time()
                        camera_info['reconnect_attempts'] = 0
                        camera_info['next_reconnect_time'] = 0.0
                        logger.info(f"[{camera_id}] ✅ Caméra RTSP reconnectée avec succès")
                        return frame
                    else:
                        last_err = "read_failed"
                        cap.release()
                else:
                    last_err = "open_failed"
                    if cap:
                        try:
                            cap.release()
                        except Exception:
                            pass
                time.sleep(0.5)
            
            # Échec: programmer prochaine fenêtre de tentative
            camera_info['reconnect_attempts'] += 1
            backoff = min(2 ** camera_info['reconnect_attempts'], 30)
            camera_info['next_reconnect_time'] = time.time() + backoff
            logger.error(f"[{camera_id}] ❌ Impossible de reconnecter la caméra (err={last_err}). Nouvelle tentative dans {backoff:.0f}s")
            # S'assurer que cap sera réouvert proprement à la prochaine tentative
            camera_info['cap'] = None
            return None
        except Exception as e:
            self.reconnect_attempts += 1
            backoff = min(2 ** self.reconnect_attempts, 30)
            self.next_reconnect_time = time.time() + backoff
            logger.exception(f"Erreur lors de la reconnexion: {e}. Nouvelle tentative dans {backoff:.0f}s")
            self.cap = None
            return None
    
    def is_active(self):
        """Vérifie si la capture est active"""
        return self.is_capturing

    def get_source_fps(self, camera_id):
        """Retourne le FPS de la source si disponible, sinon None"""
        try:
            if camera_id in self.captures:
                camera_info = self.captures[camera_id]
                cap = camera_info['cap']
                if cap and cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    if fps and fps > 0 and fps < 240:
                        return fps
        except Exception:
            pass
        return None

    def refresh_from_env(self):
        """Recharge les paramètres RTSP depuis le fichier .env et invalide le cache.
        N'arrête pas une capture en cours; les nouveaux réglages seront utilisés pour les prochaines actions.
        """
        try:
            load_dotenv(override=True)
        except Exception:
            # Même sans dotenv, continuer avec os.environ
            pass
        # Recharger la configuration RTSP multi-caméras
        self.default_rtsp_urls = []
        for i in range(6):  # Support de 6 caméras maximum
            url_key = f'DEFAULT_RTSP_URL_{i+1}' if i > 0 else 'DEFAULT_RTSP_URL'
            username_key = f'RTSP_USERNAME_{i+1}' if i > 0 else 'RTSP_USERNAME' 
            password_key = f'RTSP_PASSWORD_{i+1}' if i > 0 else 'RTSP_PASSWORD'
            name_key = f'RTSP_NAME_{i+1}' if i > 0 else 'RTSP_NAME'
            width_key = f'RTSP_WIDTH_{i+1}' if i > 0 else 'RTSP_WIDTH'
            height_key = f'RTSP_HEIGHT_{i+1}' if i > 0 else 'RTSP_HEIGHT'
            fps_key = f'RTSP_FPS_{i+1}' if i > 0 else 'RTSP_FPS'
            
            url = os.getenv(url_key, '')
            if url:  # N'ajouter que si l'URL est définie
                self.default_rtsp_urls.append({
                    'name': os.getenv(name_key, f'RTSP Camera {i+1}'),
                    'url': url,
                    'username': os.getenv(username_key, ''),
                    'password': os.getenv(password_key, ''),
                    'width': int(os.getenv(width_key, '640')),
                    'height': int(os.getenv(height_key, '480')),
                    'fps': int(os.getenv(fps_key, '15')),
                    'enabled': True
                })
        # Invalider le cache des caméras pour forcer le recalcul
        self.cameras_cache = None
        self.cache_time = 0
        logger.info("🔄 CameraService: configuration RTSP rechargée depuis .env (cache invalidé)")

    def detect_motion(self, camera_id, current_frame):
        """Détecte le mouvement entre la frame actuelle et la précédente"""
        if not self.motion_detection_enabled or camera_id not in self.captures:
            return True  # Si désactivé, considérer qu'il y a toujours du mouvement
        
        camera_info = self.captures[camera_id]
        last_frame = camera_info.get('last_frame')
        
        if last_frame is None:
            # Première frame, sauvegarder et considérer qu'il y a mouvement
            camera_info['last_frame'] = current_frame.copy()
            camera_info['motion_detected'] = True
            return True
        
        try:
            # Redimensionner pour accélérer la détection de mouvement
            small_current = cv2.resize(current_frame, (320, 240))
            small_last = cv2.resize(last_frame, (320, 240))
            
            # Convertir en niveaux de gris
            gray_current = cv2.cvtColor(small_current, cv2.COLOR_BGR2GRAY)
            gray_last = cv2.cvtColor(small_last, cv2.COLOR_BGR2GRAY)
            
            # Calculer la différence
            diff = cv2.absdiff(gray_current, gray_last)
            
            # Calculer le pourcentage de pixels qui ont changé
            total_pixels = diff.shape[0] * diff.shape[1]
            changed_pixels = np.count_nonzero(diff > 30)  # Seuil de changement
            motion_percentage = (changed_pixels / total_pixels) * 100
            
            has_motion = motion_percentage > self.motion_threshold
            
            if has_motion:
                camera_info['last_motion_time'] = time.time()
                camera_info['motion_detected'] = True
                logger.debug(f"[{camera_id}] Mouvement détecté: {motion_percentage:.1f}%")
            else:
                camera_info['motion_detected'] = False
            
            # Mettre à jour la frame de référence périodiquement
            camera_info['frame_count'] += 1
            if camera_info['frame_count'] % 30 == 0:  # Toutes les 30 frames
                camera_info['last_frame'] = current_frame.copy()
            
            return has_motion
            
        except Exception as e:
            logger.warning(f"[{camera_id}] Erreur détection mouvement: {e}")
            return True  # En cas d'erreur, considérer qu'il y a mouvement
    
    def should_analyze_frame(self, camera_id):
        """Détermine si une frame doit être analysée par l'IA basé sur plusieurs critères"""
        if camera_id not in self.captures:
            return False
        
        camera_info = self.captures[camera_id]
        current_time = time.time()
        
        # Vérifier si assez de temps s'est écoulé depuis la dernière analyse
        cache_key = f"last_analysis_{camera_id}"
        if cache_key in self.frame_cache:
            last_analysis = self.frame_cache[cache_key].get('last_analysis_time', 0)
            # Utiliser un intervalle plus long s'il n'y a pas eu de mouvement récent
            base_interval = 2.0  # Intervalle de base
            if not camera_info.get('motion_detected', True):
                # Pas de mouvement récent, rallonger l'intervalle
                time_since_motion = current_time - camera_info.get('last_motion_time', 0)
                if time_since_motion > 60:  # Plus d'une minute sans mouvement
                    base_interval = 10.0  # Analyser seulement toutes les 10 secondes
                elif time_since_motion > 30:  # Plus de 30 secondes sans mouvement
                    base_interval = 5.0   # Analyser toutes les 5 secondes
            
            if current_time - last_analysis < base_interval:
                return False
        
        return True
    
    def optimize_frame_for_ai(self, frame, camera_id):
        """Optimise une frame pour l'envoi à l'IA (compression, résolution, etc.)"""
        try:
            if frame is None:
                return None
            
            # Redimensionner intelligemment si nécessaire
            height, width = frame.shape[:2]
            if width > self.ai_max_width or height > self.ai_max_height:
                # Calculer le ratio de redimensionnement en gardant l'aspect ratio
                scale_w = self.ai_max_width / width
                scale_h = self.ai_max_height / height
                scale = min(scale_w, scale_h)
                
                new_width = int(width * scale)
                new_height = int(height * scale)
                frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
                logger.debug(f"[{camera_id}] Frame redimensionnée: {width}x{height} -> {new_width}x{new_height}")
            
            # Encoder avec qualité réduite pour économiser la bande passante
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.ai_image_quality]
            _, buffer = cv2.imencode('.jpg', frame, encode_params)
            
            return buffer
            
        except Exception as e:
            logger.warning(f"[{camera_id}] Erreur optimisation frame: {e}")
            # Fallback: encoder normalement
            _, buffer = cv2.imencode('.jpg', frame)
            return buffer
    
    def get_frame_hash(self, frame):
        """Calcule un hash rapide d'une frame pour détecter les images identiques"""
        try:
            # Redimensionner à une petite taille pour le hash
            small_frame = cv2.resize(frame, (64, 64))
            # Convertir en niveaux de gris
            gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)
            # Calculer le hash
            frame_hash = hashlib.md5(gray.tobytes()).hexdigest()
            return frame_hash
        except Exception:
            return None
    
    def is_frame_significantly_different(self, camera_id, frame):
        """Vérifie si la frame est significativement différente de la précédente analysée"""
        frame_hash = self.get_frame_hash(frame)
        if not frame_hash:
            return True  # Si on ne peut pas calculer le hash, analyser par sécurité
        
        cache_key = f"last_analysis_{camera_id}"
        if cache_key not in self.frame_cache:
            self.frame_cache[cache_key] = {}
        
        last_hash = self.frame_cache[cache_key].get('last_hash')
        if last_hash and last_hash == frame_hash:
            logger.debug(f"[{camera_id}] Frame identique à la précédente, analyse skippée")
            return False
        
        # Sauvegarder le nouveau hash
        self.frame_cache[cache_key]['last_hash'] = frame_hash
        self.frame_cache[cache_key]['last_analysis_time'] = time.time()
        
        return True
    
    def get_optimized_frame_for_ai(self, camera_id):
        """Récupère et optimise une frame pour l'analyse IA avec tous les filtres d'optimisation"""
        frame = self.get_frame(camera_id)
        if frame is None:
            return None
        
        # 1. Détection de mouvement
        if self.motion_detection_enabled and not self.detect_motion(camera_id, frame):
            logger.debug(f"[{camera_id}] Pas de mouvement détecté, analyse skippée")
            return None
        
        # 2. Vérifier si on doit analyser selon l'intervalle adaptatif
        if not self.should_analyze_frame(camera_id):
            logger.debug(f"[{camera_id}] Intervalle non écoulé, analyse skippée")
            return None
        
        # 3. Vérifier si la frame est différente de la précédente
        if not self.is_frame_significantly_different(camera_id, frame):
            return None
        
        # 4. Optimiser la frame pour l'IA
        optimized_buffer = self.optimize_frame_for_ai(frame, camera_id)
        
        logger.debug(f"[{camera_id}] Frame optimisée pour IA: {len(optimized_buffer) if optimized_buffer is not None else 0} bytes")
        
        return optimized_buffer
