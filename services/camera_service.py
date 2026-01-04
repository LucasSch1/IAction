import cv2
import threading
import time
import os
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
                            'next_reconnect_time': 0.0
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
