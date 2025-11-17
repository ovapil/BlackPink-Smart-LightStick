import socket
import struct
import threading
import time
import os
import random
import logging
import errno
from flask import Flask, render_template, request, jsonify, send_from_directory, g
from werkzeug.utils import secure_filename
import atexit

try:
    import librosa
    import soundfile
    import numpy as np
    from scipy import stats
except ImportError:
    logging.basicConfig(level=logging.ERROR); logger = logging.getLogger(); logger.error("LỖI: pip install Flask librosa soundfile numpy scipy"); exit()

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac'}
MULTICAST_GROUP = '239.1.1.1'
MULTICAST_PORT = 1234
KEEP_ALIVE_INTERVAL = 0.9

CMD_BEAT_SYNC = 0x01
CMD_FX_BLINK = 0x03 
CMD_FX_STATIC = 0x04

STATIC_COLORS_LIST = [
    (255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 255, 0), 
    (0, 255, 255), (0, 0, 255), (128, 0, 255), (255, 0, 255)
]

active_thread = None
is_syncing = False
current_sync_mode = "idle" 
current_ip = ""
last_error = ""
udp_socket = None

current_track_info = { "filename": None, "beats": [], "tempo": 0.0, "playback_start_time": 0.0, "next_beat_index": 0 }
last_packet_sent_time = 0.0
audio_queue = [] 
blink_color_index = 0

packet_counter = 0
packet_counter_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

app = Flask(__name__) 
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 
app.secret_key = 'upload_vdj_secret_v12_logging'

if not os.path.exists(UPLOAD_FOLDER):
    try: os.makedirs(UPLOAD_FOLDER); logger.info(f"Đã tạo thư mục: {os.path.abspath(UPLOAD_FOLDER)}")
    except OSError as e: logger.error(f"Không thể tạo thư mục uploads: {e}")

def allowed_file(filename): 
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def hsv_to_rgb(h, s, v):
    h_i = int(h * 6); f = h * 6 - h_i; p = v * (1 - s); q = v * (1 - f * s); t = v * (1 - (1 - f) * s)
    if h_i == 0: r, g, b = v, t, p
    elif h_i == 1: r, g, b = q, v, p
    elif h_i == 2: r, g, b = p, v, t
    elif h_i == 3: r, g, b = p, q, v
    elif h_i == 4: r, g, b = t, p, v
    elif h_i == 5: r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)

def analyze_beats(filepath, user_tempo=None):
    global last_error
    try:
        logger.info(f"Phân tích (HPSS + Cường độ): {os.path.basename(filepath)}...")
        y, sr = librosa.load(filepath, sr=None, mono=True)
        duration = librosa.get_duration(y=y, sr=sr)
        logger.info(f"Đã tải. SR: {sr} Hz, Dài: {duration:.2f}s.")
        hop_length_analysis = 512
        logger.info("Thực hiện tách Harmonic/Percussive (HPSS)...")
        y_percussive = librosa.effects.percussive(y, margin=3.0)
        logger.info("HPSS hoàn tất.")
        
        onset_env_perc = librosa.onset.onset_strength(y=y_percussive, sr=sr, hop_length=hop_length_analysis, aggregate=np.median)
        
        local_tempo = 0.0
        if user_tempo and user_tempo > 0:
            logger.info(f"Sử dụng Tempo do người dùng cung cấp: {user_tempo:.0f} BPM.")
            local_tempo = float(user_tempo)
        else:
            logger.info("Ước tính tempo từ Librosa...")
            tempo_estimate = librosa.beat.tempo(onset_envelope=onset_env_perc, sr=sr, hop_length=hop_length_analysis)
            if isinstance(tempo_estimate, np.ndarray): tempo_value = float(tempo_estimate[0]) if len(tempo_estimate) > 0 else 0.0
            else: tempo_value = float(tempo_estimate)
            local_tempo = round(tempo_value)
            logger.info(f"Tempo ước tính (làm tròn): {local_tempo:.0f} BPM.")
        
        calculated_tempo = float(local_tempo)
        beats_with_intensity = [] 

        if calculated_tempo > 0 and len(onset_env_perc) > 0:
            _, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env_perc, sr=sr, hop_length=hop_length_analysis, units='frames', start_bpm=calculated_tempo, tightness=200)
            beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length_analysis)
            beat_intensities_raw = onset_env_perc[beat_frames]
            
            max_intensity = np.max(beat_intensities_raw)
            if max_intensity > 0:
                beat_intensities_norm = beat_intensities_raw / max_intensity
                logger.info(f"Đã chuẩn hóa {len(beat_intensities_norm)} cường độ (Max: {max_intensity:.2f})")
            else:
                logger.warning("Không phát hiện cường độ, dùng 1.0 cho tất cả.")
                beat_intensities_norm = np.ones_like(beat_intensities_raw)

            beats_with_intensity = list(zip(beat_times.tolist(), beat_intensities_norm.tolist()))
            logger.info(f"Căn chỉnh {len(beats_with_intensity)} beats theo tempo {calculated_tempo:.0f} BPM.")

        if len(beats_with_intensity) == 0:
            logger.warning("Beat track thất bại, dùng onset detect dự phòng...")
            onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env_perc, sr=sr, hop_length=hop_length_analysis, units='frames', backtrack=False)
            onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length_analysis)
            beats_with_intensity = list(zip(onset_times.tolist(), np.ones_like(onset_times).tolist()))
            logger.info(f"Dự phòng cuối: Sử dụng {len(beats_with_intensity)} onsets.")
            
        last_error = ""
        return {
            "filename": os.path.basename(filepath),
            "beats": beats_with_intensity, 
            "tempo": calculated_tempo,
            "success": True
        }
    except Exception as e:
        logger.error(f"Lỗi phân tích beat (HPSS Style): {e}", exc_info=True)
        last_error = f"Lỗi phân tích file: {type(e).__name__}"
        return {"success": False, "error": last_error}

def send_udp_packet(command_byte, r, g, b):
    global udp_socket, last_packet_sent_time, last_error, is_syncing, current_ip, packet_counter, packet_counter_lock
    
    if udp_socket and is_syncing:
        
        with packet_counter_lock:
            packet_counter += 1
            current_packet_id = packet_counter
        
        message = struct.pack('<BBBB I', command_byte, r, g, b, current_packet_id)
        
        try:
            udp_socket.sendto(message, (MULTICAST_GROUP, MULTICAST_PORT))
            sent_time_ns = time.time_ns() 
            last_packet_sent_time = sent_time_ns / 1_000_000_000.0 
            
            logger.info(f"LOG,SENT,{current_packet_id},{command_byte},{r},{g},{b},{sent_time_ns}")
            
            return True
        
        except OSError as e:
            if e.errno in (errno.EADDRNOTAVAIL, errno.ENETUNREACH): logger.error(f"Lỗi UDP ({e.errno}): IP {current_ip}?. Dừng..."); last_error = f"Lỗi UDP: IP {current_ip}?"; is_syncing = False
            else: logger.error(f"Lỗi UDP khác ({e.errno}): {e}")
            return False
        except Exception as e: logger.error(f"Lỗi UDP không xác định: {e}"); return False
    return False

def start_playback_sync_thread(ip_to_bind):
    global is_syncing, last_error, udp_socket, last_packet_sent_time, current_sync_mode, current_track_info
    local_beats = current_track_info["beats"] 
    if not local_beats: 
        logger.error("Thread: No beats."); last_error = "Lỗi: No beats."; is_syncing = False; return
    local_udp_socket = None
    try:
        local_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); local_udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        local_udp_socket.bind((ip_to_bind, 0)); logger.info(f"Thread (Beat): Bind IP: {ip_to_bind}")
        local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip_to_bind))
        ttl = struct.pack('b', 1); local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        udp_socket = local_udp_socket
        current_track_info["playback_start_time"] = time.time()
        current_track_info["next_beat_index"] = 0
        last_packet_sent_time = current_track_info["playback_start_time"]
        logger.info(f"Thread (Beat): Bắt đầu gửi {len(local_beats)} beats (có cường độ)...")
        min_intensity = 0.3 
        
        while is_syncing and current_sync_mode == 'beat' and current_track_info["next_beat_index"] < len(local_beats):
            current_time = time.time()
            elapsed_time = current_time - current_track_info["playback_start_time"]
            next_beat_data = local_beats[current_track_info["next_beat_index"]]
            next_beat_time = next_beat_data[0]
            
            if elapsed_time >= next_beat_time:
                next_beat_intensity = next_beat_data[1] 
                hue = random.random() 
                value = min_intensity + ((1.0 - min_intensity) * next_beat_intensity)
                r, g, b = hsv_to_rgb(hue, 1.0, value) 
                
                if send_udp_packet(CMD_BEAT_SYNC, r, g, b): 
                    logger.debug(f"Beat {current_track_info['next_beat_index']+1} (Int: {next_beat_intensity:.2f})"); 
                    current_track_info["next_beat_index"] += 1
                else: 
                    logger.error("Thread (Beat): Lỗi gửi beat."); break
            
            elif (current_time - last_packet_sent_time) > KEEP_ALIVE_INTERVAL:
                if not send_udp_packet(CMD_BEAT_SYNC, 0, 0, 0): 
                    logger.error("Thread (Beat): Lỗi gửi keep-alive."); break
            
            sleep_time = 0.005
            if current_track_info["next_beat_index"] < len(local_beats):
                current_next_beat_time = local_beats[current_track_info["next_beat_index"]][0]
            else:
                current_next_beat_time = float('inf')
            
            time_to_next_beat = current_next_beat_time - elapsed_time
            time_to_keep_alive = (last_packet_sent_time + KEEP_ALIVE_INTERVAL) - current_time
            sleep_until = float('inf');
            
            if time_to_next_beat > 0.01: sleep_until = min(sleep_until, time_to_next_beat - 0.005)
            if time_to_keep_alive > 0.01: sleep_until = min(sleep_until, time_to_keep_alive - 0.005)
            if sleep_until != float('inf') and sleep_until > 0: sleep_time = max(0.005, sleep_until)
            
            time.sleep(sleep_time)
            
        if is_syncing and current_sync_mode == 'beat' and current_track_info["next_beat_index"] >= len(local_beats): 
            logger.info("Thread (Beat): Gửi hết beats."); 
            is_syncing = False
            
    except OSError as e: logger.error(f"Thread (Beat): LỖI SOCKET: {e}"); last_error = f"Lỗi Socket: {e}. IP?"; is_syncing = False
    except Exception as e: logger.error(f"Thread (Beat): LỖI LUỒNG: {e}", exc_info=True); last_error = f"Lỗi: {e}"; is_syncing = False
    finally:
        logger.info("Thread (Beat): Dọn dẹp...");
        if local_udp_socket: local_udp_socket.close()
        if current_sync_mode == 'beat': 
            udp_socket = None; is_syncing = False; current_sync_mode = 'idle'
            current_track_info = {"filename": None, "beats": [], "tempo": 0.0, "playback_start_time": 0.0, "next_beat_index": 0} 
        logger.info("Thread (Beat): Hoàn tất dọn dẹp.")

def start_preset_effect_thread(effect_mode, ip_to_bind):
    global is_syncing, last_error, udp_socket, last_packet_sent_time, current_sync_mode, blink_color_index
    local_udp_socket = None
    try:
        local_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); local_udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        local_udp_socket.bind((ip_to_bind, 0)); logger.info(f"Thread (Effect): Bind IP: {ip_to_bind}")
        local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip_to_bind))
        ttl = struct.pack('b', 1); local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        udp_socket = local_udp_socket
        logger.info(f"Thread (Effect): Bắt đầu hiệu ứng {effect_mode}...")
        blink_state_on = True 
        blink_color_index = 0 
        
        while is_syncing and current_sync_mode == effect_mode:
            command_byte = 0x00
            if effect_mode == 'blink':
                command_byte = CMD_FX_BLINK
                if blink_state_on:
                    r, g, b = STATIC_COLORS_LIST[blink_color_index]
                    blink_color_index = (blink_color_index + 1) % len(STATIC_COLORS_LIST)
                else:
                    r, g, b = 0, 0, 0
                blink_state_on = not blink_state_on
                sleep_time = 0.5 
            else: 
                logger.warning(f"Hiệu ứng không xác định: {effect_mode}"); break
            if not send_udp_packet(command_byte, r, g, b):
                logger.error(f"Thread (Effect): Lỗi gửi {effect_mode}."); break
            time.sleep(sleep_time)
    except OSError as e: logger.error(f"Thread (Effect): LỖI SOCKET: {e}"); last_error = f"Lỗi Socket: {e}. IP?"; is_syncing = False
    except Exception as e: logger.error(f"Thread (Effect): LỖI LUỒNG: {e}", exc_info=True); last_error = f"Lỗi: {e}"; is_syncing = False
    finally:
        logger.info(f"Thread (Effect): Dọn dẹp {effect_mode}...");
        if local_udp_socket: local_udp_socket.close()
        if current_sync_mode == effect_mode:
            udp_socket = None; is_syncing = False; current_sync_mode = 'idle'
        logger.info("Thread (Effect): Hoàn tất dọn dẹp.")

def start_static_color_thread(r, g, b, ip_to_bind):
    global is_syncing, last_error, udp_socket, last_packet_sent_time, current_sync_mode
    local_udp_socket = None
    try:
        local_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); local_udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        local_udp_socket.bind((ip_to_bind, 0)); logger.info(f"Thread (Static): Bind IP: {ip_to_bind}")
        local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip_to_bind))
        ttl = struct.pack('b', 1); local_udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        udp_socket = local_udp_socket
        logger.info(f"Thread (Static): Bắt đầu màu {r},{g},{b}...")
        while is_syncing and current_sync_mode == 'static':
            if not send_udp_packet(CMD_FX_STATIC, r, g, b):
                logger.error(f"Thread (Static): Lỗi gửi màu."); break
            time.sleep(KEEP_ALIVE_INTERVAL) 
    except OSError as e: logger.error(f"Thread (Static): LỖI SOCKET: {e}"); last_error = f"Lỗi Socket: {e}. IP?"; is_syncing = False
    except Exception as e: logger.error(f"Thread (Static): LỖI LUỒNG: {e}", exc_info=True); last_error = f"Lỗi: {e}"; is_syncing = False
    finally:
        logger.info(f"Thread (Static): Dọn dẹp...");
        if local_udp_socket: local_udp_socket.close()
        if current_sync_mode == 'static':
            udp_socket = None; is_syncing = False; current_sync_mode = 'idle'
        logger.info("Thread (Static): Hoàn tất dọn dẹp.")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    logger.debug(f"Requesting audio file: {filename}")
    response = send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)
    response.headers['Accept-Ranges'] = 'bytes' 
    return response

@app.route('/status', methods=['GET'])
def get_status():
    global last_error, audio_queue
    queue_data = []
    for track in audio_queue:
        queue_data.append({"filename": track["filename"], "tempo": track["tempo"]})
    
    server_error = last_error
    last_error = "" 
    return jsonify({
        'is_syncing': is_syncing,
        'current_sync_mode': current_sync_mode,
        'current_audio_file': current_track_info["filename"], 
        'audio_queue': queue_data, 
        'current_ip': current_ip,
        'server_error': server_error
    })

@app.route('/upload', methods=['POST'])
def upload_file():
    global audio_queue
    if 'audiofile' not in request.files:
        return jsonify({'status': 'error', 'message': 'Chưa chọn file'}), 400
    file = request.files['audiofile']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'Chưa chọn file'}), 400
        
    if file and allowed_file(file.filename): 
        filename = secure_filename(file.filename); filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        try:
            file.save(filepath); logger.info(f"Đã lưu: {filename}")
            user_tempo_str = request.form.get('tempo', '0').strip()
            user_tempo = 0.0
            if user_tempo_str:
                try: user_tempo = float(user_tempo_str)
                except ValueError: user_tempo = 0.0
            
            analysis_result = analyze_beats(filepath, user_tempo)
            
            if analysis_result["success"]:
                queue_track = {
                    "filename": analysis_result["filename"],
                    "beats": analysis_result["beats"] if len(analysis_result["beats"]) < 5000 else analysis_result["beats"][:5000],
                    "tempo": analysis_result["tempo"]
                }
                if len(analysis_result["beats"]) >= 5000:
                    logger.warning(f"File quá dài, chỉ lưu 5000 beats đầu tiên.")

                audio_queue.append(queue_track) 
                logger.info(f"Đã thêm '{analysis_result['filename']}' vào hàng đợi. Queue size: {len(audio_queue)}")
                
                return jsonify({
                    'status': 'success', 
                    'message': f"Phân tích '{analysis_result['filename']}' OK.",
                    'filename': analysis_result['filename'],
                    'beats': len(analysis_result['beats']),
                    'tempo': analysis_result['tempo']
                })
            else:
                return jsonify({'status': 'error', 'message': analysis_result["error"]}), 400
        except Exception as e:
            logger.error(f"Lỗi lưu/phân tích file: {e}"); 
            return jsonify({'status': 'error', 'message': f'Lỗi lưu file: {e}'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'Định dạng file không hợp lệ'}), 400

def stop_sending_internal():
    global is_syncing, active_thread, current_sync_mode, current_track_info
    if is_syncing or active_thread is not None:
        logger.info(f"Dừng thread nội bộ (mode: {current_sync_mode})...");
        is_syncing = False
        current_sync_mode = 'idle'
        if active_thread and active_thread.is_alive():
            active_thread.join(timeout=1.0)
            if active_thread.is_alive(): logger.warning("Thread không dừng kịp!")
        active_thread = None
        current_track_info = {"filename": None, "beats": [], "tempo": 0.0, "playback_start_time": 0.0, "next_beat_index": 0}

@app.route('/start_beat', methods=['POST'])
def start_beat_sync():
    global active_thread, is_syncing, current_ip, last_error, current_sync_mode, audio_queue, current_track_info
    data = request.json
    req_ip = data.get('ip', '').strip()
    if not req_ip:
        return jsonify({'status': 'error', 'message': 'IP trống'}), 400
    if is_syncing:
        return jsonify({'status': 'error', 'message': 'Hệ thống đang bận, vui lòng Dừng trước'}), 400
    if not audio_queue:
        return jsonify({'status': 'error', 'message': 'Hàng đợi trống. Vui lòng upload file nhạc.'}), 400
    stop_sending_internal(); time.sleep(0.1) 
    track_to_play = audio_queue.pop(0) 
    current_track_info["filename"] = track_to_play["filename"]
    current_track_info["beats"] = track_to_play["beats"]
    current_track_info["tempo"] = track_to_play["tempo"]
    current_ip = req_ip
    logger.info(f"Yêu cầu START BEAT sync IP {current_ip} file {current_track_info['filename']}");
    is_syncing = True; last_error = ""; current_sync_mode = 'beat'
    active_thread = threading.Thread(target=start_playback_sync_thread, args=(current_ip,), name="PlaybackSyncThread");
    active_thread.daemon = True; active_thread.start()
    return jsonify({
        'status': 'success', 
        'message': f"Bắt đầu đồng bộ BEAT: {current_track_info['filename']}",
        'filename': current_track_info['filename'] 
    })

@app.route('/queue/delete', methods=['POST'])
def delete_from_queue():
    global audio_queue
    data = request.json
    filename_to_delete = data.get('filename')
    if not filename_to_delete:
        return jsonify({'status': 'error', 'message': 'Thiếu tên file'}), 400
    original_len = len(audio_queue)
    audio_queue = [track for track in audio_queue if track['filename'] != filename_to_delete]
    new_len = len(audio_queue)
    if new_len < original_len:
        logger.info(f"Đã xóa '{filename_to_delete}' khỏi hàng đợi.")
        return jsonify({'status': 'success', 'message': f"Đã xóa '{filename_to_delete}'."})
    else:
        logger.warning(f"Không tìm thấy file '{filename_to_delete}' để xóa.")
        return jsonify({'status': 'error', 'message': 'Không tìm thấy file trong hàng đợi'}), 404

@app.route('/set_color', methods=['POST'])
def set_static_color():
    global active_thread, is_syncing, current_ip, last_error, current_sync_mode
    data = request.json
    req_ip = data.get('ip', '').strip()
    if not req_ip:
        return jsonify({'status': 'error', 'message': 'IP trống'}), 400
    if is_syncing:
        return jsonify({'status': 'error', 'message': 'Hệ thống đang bận, vui lòng Dừng trước'}), 400
    try:
        r = int(data.get('r', 0)); g = int(data.get('g', 0)); b = int(data.get('b', 0))
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Màu không hợp lệ'}), 400
    stop_sending_internal(); time.sleep(0.1)
    current_ip = req_ip
    logger.info(f"Yêu cầu START STATIC COLOR {r},{g},{b} IP {current_ip}");
    is_syncing = True; last_error = ""; current_sync_mode = 'static'
    active_thread = threading.Thread(target=start_static_color_thread, args=(r, g, b, current_ip,), name="StaticColorThread");
    active_thread.daemon = True; active_thread.start()
    return jsonify({'status': 'success', 'message': f"Bắt đầu màu tĩnh: {r},{g},{b}"})

@app.route('/start_effect', methods=['POST'])
def start_effect_sync():
    global active_thread, is_syncing, current_ip, last_error, current_sync_mode
    data = request.json
    req_ip = data.get('ip', '').strip()
    effect_name = data.get('effect_name')
    if not req_ip: return jsonify({'status': 'error', 'message': 'IP trống'}), 400
    if effect_name not in ['blink']: 
        return jsonify({'status': 'error', 'message': 'Hiệu ứng không hợp lệ'}), 400
    if is_syncing:
        return jsonify({'status': 'error', 'message': 'Hệ thống đang bận, vui lòng Dừng trước'}), 400
    stop_sending_internal(); time.sleep(0.1)
    current_ip = req_ip
    logger.info(f"Yêu cầu START EFFECT {effect_name} IP {current_ip}");
    is_syncing = True; last_error = ""; current_sync_mode = effect_name
    active_thread = threading.Thread(target=start_preset_effect_thread, args=(effect_name, current_ip,), name="PresetEffectThread");
    active_thread.daemon = True; active_thread.start()
    return jsonify({'status': 'success', 'message': f"Bắt đầu hiệu ứng: {effect_name}"})

@app.route('/stop', methods=['POST'])
def stop_sending():
    stop_sending_internal()
    logger.info("Yêu cầu DỪNG TỪ CLIENT.")
    return jsonify({'status': 'success', 'message': 'Đã dừng đồng bộ.'})

def shutdown_server():
    logger.info("Server đang tắt..."); stop_sending_internal()
    logger.info("Đã dừng các tác vụ.")
if __name__ == '__main__':
    logger.info("Khởi động Web Server...")
    logger.info(f"Thư mục Uploads: {os.path.abspath(UPLOAD_FOLDER)}")
    logger.info("Truy cập http://127.0.0.1:5000 (hoặc IP mạng của bạn)")
    atexit.register(shutdown_server)
    try:
        app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False) 
    except KeyboardInterrupt: logger.info("Nhận Ctrl+C.")
    finally:
        shutdown_server()