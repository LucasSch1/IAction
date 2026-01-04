import uuid
import time
import threading
import json
import os
from typing import Dict, List, Any, Optional
import logging
import requests

logger = logging.getLogger(__name__)

class DetectionService:
    def __init__(self, ai_service, mqtt_service):
        self.ai_service = ai_service
        self.mqtt_service = mqtt_service
        self.detections = {}
        self.lock = threading.Lock()
        self.detections_file = 'detections.json'
        
        # États des binary sensors pour éviter les publications répétées (par caméra)
        self.binary_sensor_states = {}  # camera_id -> {detection_id: boolean}
        self.last_analysis_results = {}  # camera_id -> results
        
        # Gestion de l'intervalle minimum entre analyses (global et par caméra)
        self.last_analysis_time = {}  # camera_id -> timestamp
        self.min_analysis_interval = float(os.getenv('MIN_ANALYSIS_INTERVAL', '0.1'))  # Intervalle global par défaut
        self.camera_analysis_intervals = {}  # camera_id -> interval personnalisé
        
        # Charger les intervalles personnalisés depuis l'environnement
        self._load_camera_intervals()
        
        # Charger les détections sauvegardées
        self.load_detections()
    
    def _load_camera_intervals(self):
        """Charge les intervalles d'analyse personnalisés par caméra"""
        # Support pour 6 caméras avec intervalles personnalisés
        for i in range(6):
            interval_key = f'MIN_ANALYSIS_INTERVAL_{i+1}' if i > 0 else 'MIN_ANALYSIS_INTERVAL_1'
            camera_key = f'CAMERA_ID_{i+1}' if i > 0 else 'CAMERA_ID_1'
            
            interval = os.getenv(interval_key)
            camera_id = os.getenv(camera_key)
            
            if interval and camera_id:
                try:
                    self.camera_analysis_intervals[camera_id] = float(interval)
                    logger.info(f"📊 Caméra {camera_id}: intervalle d'analyse = {interval}s")
                except ValueError:
                    logger.warning(f"⚠️ Intervalle invalide pour {camera_id}: {interval}")
    
    def get_camera_analysis_interval(self, camera_id: str) -> float:
        """Récupère l'intervalle d'analyse pour une caméra spécifique"""
        return self.camera_analysis_intervals.get(camera_id, self.min_analysis_interval)
    
    def add_detection(self, name: str, phrase: str, webhook_url: Optional[str] = None, enabled_cameras: Optional[List[str]] = None) -> str:
        """Ajoute une nouvelle détection personnalisée avec webhook optionnel"""
        with self.lock:
            detection_id = str(uuid.uuid4())
            
            # Si aucune caméra spécifiée, utiliser toutes les caméras existantes
            if enabled_cameras is None:
                enabled_cameras = list(self.binary_sensor_states.keys())
            
            self.detections[detection_id] = {
                'id': detection_id,
                'name': name,
                'phrase': phrase,
                'webhook_url': webhook_url,
                'enabled_cameras': enabled_cameras,
                'created_at': time.time(),
                'last_triggered': None,
                'trigger_count': 0
            }
            
            # Configurer les binary sensors MQTT uniquement pour les caméras sélectionnées
            for camera_id in enabled_cameras:
                if camera_id in self.binary_sensor_states:
                    sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                    sensor_name = f"Détection {name} ({camera_id})"
                    self.mqtt_service.setup_binary_sensor(
                        sensor_id=sensor_id,
                        name=sensor_name,
                        device_class="motion"
                    )
                    
                    # Initialiser l'état pour cette caméra
                    self.binary_sensor_states[camera_id][detection_id] = False
                    self.mqtt_service.buffer_binary_sensor_state(sensor_id, False)
            
            self.mqtt_service.flush_message_buffer()
            
            # Sauvegarder les détections
            self.save_detections()
            
            return detection_id
    
    def remove_detection(self, detection_id: str) -> bool:
        """Supprime une détection"""
        with self.lock:
            if detection_id not in self.detections:
                return False
            
            # Supprimer tous les binary sensors MQTT de toutes les caméras
            detection = self.detections[detection_id]
            enabled_cameras = detection.get('enabled_cameras', list(self.binary_sensor_states.keys()))
            
            for camera_id in enabled_cameras:
                if camera_id in self.binary_sensor_states:
                    sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                    self.mqtt_service.remove_sensor(sensor_id, "binary_sensor")
            
            # Supprimer de nos structures
            del self.detections[detection_id]
            # Supprimer des états par caméra
            for camera_id in self.binary_sensor_states:
                if detection_id in self.binary_sensor_states[camera_id]:
                    del self.binary_sensor_states[camera_id][detection_id]
            if detection_id in self.last_analysis_results:
                del self.last_analysis_results[detection_id]
            
            # Sauvegarder les détections
            self.save_detections()
            
            return True
    
    def get_detections(self) -> List[Dict[str, Any]]:
        """Récupère la liste des détections"""
        with self.lock:
            return list(self.detections.values())

    def update_detection(self, detection_id: str, name: Optional[str] = None, phrase: Optional[str] = None, webhook_url: Optional[str] = None, enabled_cameras: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Met à jour une détection (nom, phrase, webhook, caméras)"""
        with self.lock:
            if detection_id not in self.detections:
                return None
            det = self.detections[detection_id]
            changed_name = False
            changed_cameras = False
            
            if name is not None and name.strip() and name != det.get('name'):
                det['name'] = name.strip()
                changed_name = True
            if phrase is not None and phrase.strip():
                det['phrase'] = phrase.strip()
            # webhook_url peut être vide pour supprimer
            if webhook_url is not None:
                webhook_url = webhook_url.strip()
                det['webhook_url'] = webhook_url if webhook_url else None
            
            # Gérer les changements de caméras
            if enabled_cameras is not None:
                old_cameras = set(det.get('enabled_cameras', []))
                new_cameras = set(enabled_cameras)
                
                if old_cameras != new_cameras:
                    det['enabled_cameras'] = enabled_cameras
                    changed_cameras = True
                    
                    # Supprimer les sensors des anciennes caméras
                    for camera_id in old_cameras - new_cameras:
                        if camera_id in self.binary_sensor_states:
                            # Supprimer le sensor MQTT de Home Assistant
                            sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                            self.mqtt_service.remove_sensor(sensor_id, "binary_sensor")
                            # Supprimer de notre état local
                            self.binary_sensor_states[camera_id].pop(detection_id, None)
                    
                    # Ajouter les sensors pour les nouvelles caméras
                    for camera_id in new_cameras - old_cameras:
                        if camera_id in self.binary_sensor_states:
                            sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                            sensor_name = f"Détection {det['name']} ({camera_id})"
                            self.mqtt_service.setup_binary_sensor(
                                sensor_id=sensor_id,
                                name=sensor_name,
                                device_class="motion"
                            )
                            self.binary_sensor_states[camera_id][detection_id] = False
                            self.mqtt_service.buffer_binary_sensor_state(sensor_id, False)
            
            # Reconfigurer les binary sensors si le nom a changé
            if changed_name:
                enabled_cameras_list = det.get('enabled_cameras', list(self.binary_sensor_states.keys()))
                for camera_id in enabled_cameras_list:
                    if camera_id in self.binary_sensor_states:
                        sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                        sensor_name = f"Détection {det['name']} ({camera_id})"
                        self.mqtt_service.setup_binary_sensor(
                            sensor_id=sensor_id,
                            name=sensor_name,
                            device_class="motion"
                        )
                        
            if changed_cameras:
                self.mqtt_service.flush_message_buffer()
            
            self.save_detections()
            return det.copy()
    
    def analyze_frame(self, image_base64: str, camera_id: str = "default") -> dict:
        """Analyse une image avec toutes les détections configurées
        
        Args:
            image_base64: Image encodée en base64
            camera_id: Identifiant de la caméra (pour logs et MQTT)
            
        Returns:
            dict: Résultats de l'analyse avec la clé 'detections' uniquement
        """
        current_time = time.time()
        
        # Récupérer l'intervalle personnalisé pour cette caméra
        camera_interval = self.get_camera_analysis_interval(camera_id)
        last_analysis_time = self.last_analysis_time.get(camera_id, 0)
        
        # Vérifier l'intervalle minimum entre analyses pour cette caméra spécifique
        if current_time - last_analysis_time < camera_interval:
            # Retourner les derniers résultats si l'intervalle n'est pas respecté
            if camera_id in self.last_analysis_results:
                return self.last_analysis_results[camera_id]
            else:
                return {
                    'detections': [],
                    'success': True,
                    'timestamp': current_time,
                    'skipped': True,  # Indicateur que l'analyse a été ignorée
                    'camera_id': camera_id,
                    'next_analysis_in': camera_interval - (current_time - last_analysis_time)
                }
        
        results = {
            'detections': [],
            'success': True,
            'timestamp': current_time
        }
        
        try:
            # Initialiser les états pour cette caméra si nécessaire
            if camera_id not in self.binary_sensor_states:
                self.binary_sensor_states[camera_id] = {}
            
            # Récupérer la liste des détections personnalisées pour cette caméra
            with self.lock:
                detections_list = []
                for detection_id, detection in self.detections.items():
                    enabled_cameras = detection.get('enabled_cameras', [])
                    # Si aucune caméra spécifiée (ancienne détection) ou si cette caméra est dans la liste
                    if not enabled_cameras or camera_id in enabled_cameras:
                        detections_list.append({
                            'id': detection_id,
                            'phrase': detection['phrase'],
                            'name': detection['name']
                        })
            
            if not detections_list:
                return {
                    'success': True,
                    'analysis_count': 0,
                    'detections': []
                }
            
            # Utiliser la méthode d'analyse combinée pour tout analyser en un seul appel
            combined_results = self.ai_service.analyze_combined(image_base64, detections_list)
            
            if combined_results['success']:
                # Traiter les résultats des détections personnalisées
                if 'detections' in combined_results:
                    detection_results = []
                    for detection_result in combined_results['detections']:
                        detection_id = detection_result['id']
                        is_match = detection_result['match']
                        
                        # Mettre à jour l'état du binary sensor si nécessaire (par caméra)
                        if detection_id in self.detections:
                            sensor_id = f"detection_{detection_id.replace('-', '_')}"
                            
                            # Vérifier si l'état a changé pour cette caméra spécifique
                            camera_states = self.binary_sensor_states[camera_id]
                            previous_state = camera_states.get(detection_id, False)
                            
                            if previous_state != is_match:
                                camera_states[detection_id] = is_match
                                
                                # Pour MQTT, utiliser un sensor ID spécifique par caméra
                                camera_sensor_id = f"{sensor_id}_{camera_id.replace('-', '_')}"
                                self.mqtt_service.buffer_binary_sensor_state(camera_sensor_id, is_match)
                            
                            # Mettre à jour les statistiques de la détection et déclencher webhook si configuré
                            if is_match:
                                with self.lock:
                                    if detection_id in self.detections:
                                        current_time = time.time()
                                        self.detections[detection_id]['last_triggered'] = current_time
                                        self.detections[detection_id]['trigger_count'] += 1
                                        webhook_url = self.detections[detection_id].get('webhook_url')
                                        if webhook_url:
                                            try:
                                                threading.Thread(
                                                    target=self._trigger_webhook,
                                                    args=(
                                                        detection_id,
                                                        self.detections[detection_id]['name'],
                                                        webhook_url,
                                                        True,
                                                        current_time,
                                                    ),
                                                    daemon=True
                                                ).start()
                                            except Exception as e:
                                                logger.debug(f"Erreur lancement webhook pour '{self.detections[detection_id]['name']}': {e}")
                            
                            # Ajouter aux résultats
                            detection_results.append({
                                'id': detection_id,
                                'name': self.detections[detection_id]['name'],
                                'match': is_match,
                                'success': True
                            })
                    
                    results['detections'] = detection_results
            else:
                # En cas d'erreur dans l'analyse combinée
                results['success'] = False
                results['error'] = combined_results.get('error', 'Erreur inconnue dans l\'analyse combinée')
            
            # Sauvegarder les résultats pour référence (par caméra)
            self.last_analysis_results[camera_id] = results.copy()
            
            # Mettre à jour le timestamp de la dernière analyse pour cette caméra
            self.last_analysis_time[camera_id] = current_time
            
            # Envoyer tous les messages MQTT en une seule fois
            self.mqtt_service.flush_message_buffer()
            
            return results
            
        except Exception as e:
            logger.error(f"[{camera_id}] Erreur lors de l'analyse de l'image: {e}")
            results['success'] = False
            results['error'] = str(e)
            return results
    
    def register_camera(self, camera_id: str):
        """Enregistre une nouvelle caméra et crée les sensors MQTT associés"""
        if camera_id not in self.binary_sensor_states:
            self.binary_sensor_states[camera_id] = {}
            
            # Créer les sensors MQTT pour les détections qui incluent cette caméra
            with self.lock:
                for detection_id, detection in self.detections.items():
                    enabled_cameras = detection.get('enabled_cameras', [])
                    # Si la détection n'a pas de caméras spécifiées ou si cette caméra est dans la liste
                    if not enabled_cameras or camera_id in enabled_cameras:
                        sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                        sensor_name = f"Détection {detection['name']} ({camera_id})"
                        self.mqtt_service.setup_binary_sensor(
                            sensor_id=sensor_id,
                            name=sensor_name,
                            device_class="motion"
                        )
                        # Initialiser l'état à False
                        self.binary_sensor_states[camera_id][detection_id] = False
    
    def cleanup_mqtt_sensors(self):
        """Nettoie les sensors MQTT obsolètes et synchronise avec l'état actuel"""
        logger.info("🧹 Nettoyage des sensors MQTT...")
        
        with self.lock:
            # Pour chaque caméra enregistrée
            for camera_id in list(self.binary_sensor_states.keys()):
                # Pour chaque détection dans les états de cette caméra
                for detection_id in list(self.binary_sensor_states[camera_id].keys()):
                    if detection_id not in self.detections:
                        # Détection supprimée - nettoyer le sensor
                        sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                        logger.info(f"🗑️ Suppression sensor orphelin: {sensor_id}")
                        self.mqtt_service.remove_sensor(sensor_id, "binary_sensor")
                        del self.binary_sensor_states[camera_id][detection_id]
                    else:
                        # Vérifier si cette caméra est toujours activée pour cette détection
                        detection = self.detections[detection_id]
                        enabled_cameras = detection.get('enabled_cameras', [])
                        
                        # Si la détection a des caméras spécifiées et cette caméra n'y est pas
                        if enabled_cameras and camera_id not in enabled_cameras:
                            sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                            logger.info(f"🗑️ Suppression sensor désactivé: {sensor_id}")
                            self.mqtt_service.remove_sensor(sensor_id, "binary_sensor")
                            del self.binary_sensor_states[camera_id][detection_id]
            
            # S'assurer que toutes les détections ont leurs sensors sur les bonnes caméras
            for detection_id, detection in self.detections.items():
                enabled_cameras = detection.get('enabled_cameras', list(self.binary_sensor_states.keys()))
                
                for camera_id in enabled_cameras:
                    if camera_id in self.binary_sensor_states:
                        if detection_id not in self.binary_sensor_states[camera_id]:
                            # Ajouter le sensor manquant
                            sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                            sensor_name = f"Détection {detection['name']} ({camera_id})"
                            logger.info(f"➕ Ajout sensor manquant: {sensor_id}")
                            self.mqtt_service.setup_binary_sensor(
                                sensor_id=sensor_id,
                                name=sensor_name,
                                device_class="motion"
                            )
                            self.binary_sensor_states[camera_id][detection_id] = False
                            self.mqtt_service.buffer_binary_sensor_state(sensor_id, False)
            
            self.mqtt_service.flush_message_buffer()
            logger.info("✅ Nettoyage des sensors MQTT terminé")
    
    # Les méthodes _analyze_fixed_sensors et _analyze_custom_detections ont été supprimées
    # car elles sont remplacées par l'utilisation de la méthode analyze_combined du service AI
    
    def get_detection_status(self, detection_id: str, camera_id: str = None) -> Dict[str, Any]:
        """Récupère le statut d'une détection"""
        with self.lock:
            if detection_id not in self.detections:
                return None
            
            detection = self.detections[detection_id].copy()
            
            if camera_id is not None:
                # Statut pour une caméra spécifique
                detection['current_state'] = self.binary_sensor_states.get(camera_id, {}).get(detection_id, False)
                detection['camera_id'] = camera_id
            else:
                # Statut global - True si la détection est active sur au moins une caméra
                detection['current_state'] = any(
                    cam_states.get(detection_id, False) 
                    for cam_states in self.binary_sensor_states.values()
                )
                detection['active_cameras'] = [
                    cam_id for cam_id, cam_states in self.binary_sensor_states.items()
                    if cam_states.get(detection_id, False)
                ]
            
            detection['last_analysis'] = self.last_analysis_results.get(detection_id)
            
            return detection
    
    def get_all_status(self, camera_id: str = None) -> Dict[str, Any]:
        """Récupère le statut de toutes les détections"""
        with self.lock:
            if camera_id is not None:
                # Statut pour une caméra spécifique
                status = {
                    'camera_id': camera_id,
                    'total_detections': len(self.detections),
                    'active_detections': sum(
                        1 for detection_id in self.detections 
                        if self.binary_sensor_states.get(camera_id, {}).get(detection_id, False)
                    ),
                    'detections': []
                }
                
                for detection_id in self.detections:
                    detection_status = self.get_detection_status(detection_id, camera_id)
                    if detection_status:
                        status['detections'].append(detection_status)
            else:
                # Statut global
                status = {
                    'total_detections': len(self.detections),
                    'active_detections': sum(
                        1 for detection_id in self.detections 
                        if any(cam_states.get(detection_id, False) for cam_states in self.binary_sensor_states.values())
                    ),
                    'detections': []
                }
                
                for detection_id in self.detections:
                    detection_status = self.get_detection_status(detection_id)
                    if detection_status:
                        status['detections'].append(detection_status)
            
            return status
    
    def save_detections(self):
        """Sauvegarde les détections dans un fichier JSON"""
        try:
            # Préparer les données pour la sérialisation
            detections_data = {}
            for detection_id, detection in self.detections.items():
                detections_data[detection_id] = {
                    'id': detection['id'],
                    'name': detection['name'],
                    'phrase': detection['phrase'],
                    'webhook_url': detection.get('webhook_url'),
                    'created_at': detection['created_at'],
                    'last_triggered': detection['last_triggered'],
                    'trigger_count': detection['trigger_count']
                }
            
            with open(self.detections_file, 'w', encoding='utf-8') as f:
                json.dump(detections_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ Détections sauvegardées: {len(detections_data)} détections")
            
        except Exception as e:
            logger.error(f"⚠️ Erreur lors de la sauvegarde des détections: {e}")
    
    def load_detections(self):
        """Charge les détections depuis le fichier JSON"""
        try:
            if not os.path.exists(self.detections_file):
                logger.info("📁 Aucun fichier de détections trouvé, démarrage avec une liste vide")
                return
            
            with open(self.detections_file, 'r', encoding='utf-8') as f:
                detections_data = json.load(f)
            
            # Restaurer les détections
            for detection_id, detection in detections_data.items():
                # Compat ancien format: garantir la présence de toutes les clés
                self.detections[detection_id] = {
                    'id': detection.get('id', detection_id),
                    'name': detection.get('name', ''),
                    'phrase': detection.get('phrase', ''),
                    'webhook_url': detection.get('webhook_url'),
                    'enabled_cameras': detection.get('enabled_cameras', []),  # Nouveau champ
                    'created_at': detection.get('created_at', time.time()),
                    'last_triggered': detection.get('last_triggered'),
                    'trigger_count': detection.get('trigger_count', 0)
                }
                
                # Configurer le binary sensor MQTT
                sensor_id = f"detection_{detection_id.replace('-', '_')}"
                self.mqtt_service.setup_binary_sensor(
                    sensor_id=sensor_id,
                    name=f"Détection: {detection['name']}",
                    device_class="motion"
                )
                
                # Initialiser l'état du binary sensor
                self.binary_sensor_states[detection_id] = False
                self.mqtt_service.buffer_binary_sensor_state(sensor_id, False)
            
            if detections_data:
                self.mqtt_service.flush_message_buffer()
            
            logger.info(f"✅ Détections chargées: {len(detections_data)} détections")
            
        except Exception as e:
            logger.error(f"⚠️ Erreur lors du chargement des détections: {e}")
            logger.info("📁 Démarrage avec une liste vide")
    
    def _trigger_webhook(self, detection_id: str, detection_name: str, webhook_url: str, triggered: bool, timestamp: float):
        """Envoie un webhook HTTP POST avec un timeout court"""
        try:
            payload = {
                'detection_id': detection_id,
                'detection_name': detection_name,
                'triggered': triggered,
                'timestamp': timestamp
            }
            requests.post(webhook_url, json=payload, timeout=3)
            logger.debug(f"Webhook envoyé pour '{detection_name}' → {webhook_url}")
        except Exception as e:
            logger.debug(f"Webhook échec pour '{detection_name}' → {webhook_url}: {e}")
    
    def reconfigure_mqtt_sensors(self):
        """Reconfigure les binary sensors MQTT pour toutes les détections et nettoie les sensors obsolètes"""
        try:
            # D'abord nettoyer les sensors obsolètes
            self.cleanup_mqtt_sensors()
            
            # Puis reconfigurer tous les sensors actifs
            with self.lock:
                for detection_id, detection in self.detections.items():
                    enabled_cameras = detection.get('enabled_cameras', list(self.binary_sensor_states.keys()))
                    
                    for camera_id in enabled_cameras:
                        if camera_id in self.binary_sensor_states:
                            sensor_id = f"detection_{detection_id.replace('-', '_')}_{camera_id.replace('-', '_')}"
                            sensor_name = f"Détection {detection['name']} ({camera_id})"
                            self.mqtt_service.setup_binary_sensor(
                                sensor_id=sensor_id,
                                name=sensor_name,
                                device_class="motion"
                            )
                            # Publier l'état courant (par défaut False)
                            current_state = self.binary_sensor_states[camera_id].get(detection_id, False)
                            self.mqtt_service.buffer_binary_sensor_state(sensor_id, current_state)
                            
            self.mqtt_service.flush_message_buffer()
            logger.info("✅ Reconfiguration MQTT des détections terminée")
        except Exception as e:
            logger.error(f"⚠️ Erreur reconfiguration MQTT des détections: {e}")
    
