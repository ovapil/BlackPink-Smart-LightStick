document.addEventListener('DOMContentLoaded', () => {
    const ipInput = document.getElementById('ip-input');
    const uploadForm = document.getElementById('upload-form');
    const audioFileInput = document.getElementById('audio-file-input');
    const fileNameDisplay = document.getElementById('file-name-display');
    const tempoInput = document.getElementById('tempo-input-field');
    const uploadButton = document.getElementById('upload-button');
    
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    
    const nowPlayingText = document.getElementById('now-playing-text');
    const audioPlayer = document.getElementById('audio-player'); 
    const queueList = document.getElementById('queue-list');
    
    const palette = document.getElementById('color-palette');
    const syncButton = document.getElementById('sync-button');
    const stopButton = document.getElementById('stop-button');
    const errorContainer = document.getElementById('error-container');

    let currentIP = localStorage.getItem('lightstickIP') || '';
    ipInput.value = currentIP;

    ipInput.addEventListener('change', () => {
        currentIP = ipInput.value.trim();
        localStorage.setItem('lightstickIP', currentIP);
        showError(currentIP ? `Đã lưu IP: ${currentIP}` : 'Đã xóa IP', 'success');
    });

    audioFileInput.addEventListener('change', () => {
        if (audioFileInput.files.length > 0) {
            fileNameDisplay.textContent = audioFileInput.files[0].name;
        } else {
            fileNameDisplay.textContent = 'Chưa chọn file';
        }
    });

    let fakeProgressInterval = null;
    
    uploadForm.addEventListener('submit', async (e) => {
        e.preventDefault(); 

        if (audioFileInput.files.length === 0) {
            showError('Vui lòng chọn một file âm thanh.');
            return;
        }

        const formData = new FormData();
        formData.append('audiofile', audioFileInput.files[0]);
        formData.append('tempo', tempoInput.value || '0');

        progressContainer.style.display = 'block';
        uploadButton.disabled = true;
        progressBar.style.width = '0%';
        progressText.textContent = 'Đang tải lên... 0%';

        const startTime = Date.now();
        const duration = 30000;

        fakeProgressInterval = setInterval(() => {
            const elapsed = Date.now() - startTime;
            let progress = elapsed / duration;
            
            if (progress >= 1) {
                progress = 1;
            }
            
            const displayProgress = Math.floor(progress * 90);
            progressBar.style.width = `${displayProgress}%`;
            progressText.textContent = `Đang phân tích... ${displayProgress}%`;
            
            if (displayProgress >= 90) {
                 clearInterval(fakeProgressInterval);
            }
        }, 100);

        try {
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData,
            });

            clearInterval(fakeProgressInterval);

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.message || 'Lỗi không xác định từ server.');
            }

            const result = await response.json();

            progressBar.style.width = '100%';
            progressText.textContent = `Phân tích thành công: ${result.filename} (${Math.round(result.tempo)} BPM)`;
            
            setTimeout(() => {
                progressContainer.style.display = 'none';
                uploadButton.disabled = false;
                uploadForm.reset();
                fileNameDisplay.textContent = 'Chưa chọn file';
            }, 2000);

            pollStatus();

        } catch (error) {
            clearInterval(fakeProgressInterval); 
            progressBar.style.width = '0%';
            progressContainer.style.display = 'none';
            uploadButton.disabled = false;
            showError(`Lỗi Upload: ${error.message}`);
        }
    });

    syncButton.addEventListener('click', () => {
        if (!checkIP()) return;
        
        apiPost('/start_beat', { ip: currentIP })
            .then((result) => {
                if (result.filename) {
                    audioPlayer.src = `/uploads/${encodeURIComponent(result.filename)}`;
                    audioPlayer.play();
                }
                pollStatus(); 
            })
            .catch(err => showError(err.message));
    });

    stopButton.addEventListener('click', () => {
        apiPost('/stop', {})
            .then(() => {
                audioPlayer.pause();
                audioPlayer.src = ""; 
                pollStatus();
            })
            .catch(err => showError(err.message));
    });

    audioPlayer.addEventListener('ended', () => {
        console.log("Audio player finished.");
        stopButton.click();
    });

    palette.addEventListener('click', (e) => {
        const target = e.target.closest('.color-box');
        if (!target) return;
        if (!checkIP()) return;

        audioPlayer.pause();
        audioPlayer.src = "";

        const effect = target.dataset.effect;
        const color = target.dataset.color;

        if (effect) {
            apiPost('/start_effect', { ip: currentIP, effect_name: effect })
                .then(() => pollStatus())
                .catch(err => showError(err.message));
        } else if (color) {
            const [r, g, b] = color.split(',').map(Number);
            apiPost('/set_color', { ip: currentIP, r, g, b })
                .then(() => pollStatus())
                .catch(err => showError(err.message));
        }
    });

    queueList.addEventListener('click', (e) => {
        const deleteBtn = e.target.closest('.queue-item-delete');
        if (deleteBtn) {
            const filename = deleteBtn.dataset.filename;
            if (confirm(`Bạn có chắc muốn xóa "${filename}" khỏi hàng đợi?`)) {
                apiPost('/queue/delete', { filename: filename })
                    .then(() => {
                        showError(`Đã xóa "${filename}".`, 'success');
                        pollStatus();
                    })
                    .catch(err => showError(err.message));
            }
        }
    });

    async function apiPost(endpoint, data) {
        try {
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.message || 'Lỗi không xác định');
            }
            return await response.json();
        } catch (error) {
            console.error(`Lỗi tại ${endpoint}:`, error);
            showError(error.message); 
            throw error; 
        }
    }

    function checkIP() {
        currentIP = ipInput.value.trim(); 
        if (!currentIP) {
            showError('Vui lòng nhập địa chỉ IP của LightStick trước.');
            ipInput.focus();
            return false;
        }
        return true;
    }

    function showError(message, type = 'error') {
        errorContainer.textContent = message;
        errorContainer.style.backgroundColor = (type === 'error') ? 'var(--btn-red)' : 'var(--btn-green)';
        errorContainer.style.display = 'block';
        setTimeout(() => { errorContainer.style.display = 'none'; }, 3000);
    }

    async function pollStatus() {
        try {
            const response = await fetch('/status');
            if (!response.ok) throw new Error('Mất kết nối server');
            
            const data = await response.json();

            if (data.is_syncing) {
                syncButton.disabled = true;
                stopButton.disabled = false;
                let statusText = "Đang đồng bộ...";
                if (data.current_sync_mode === 'beat' && data.current_audio_file) {
                    statusText = data.current_audio_file;
                } else if (data.current_sync_mode === 'static') {
                    statusText = "Màu tĩnh";
                } else if (data.current_sync_mode === 'blink') {
                    statusText = "Flashy (Đa màu)";
                }
                nowPlayingText.textContent = statusText;
            } else {
                syncButton.disabled = (data.audio_queue.length === 0);
                stopButton.disabled = true;
                nowPlayingText.textContent = "Đã dừng";
                
                if (!audioPlayer.paused && data.current_sync_mode !== 'beat') {
                    audioPlayer.pause();
                    audioPlayer.src = "";
                }
            }

            queueList.innerHTML = ''; 
            if (data.audio_queue.length === 0) {
                queueList.innerHTML = '<li class="queue-item-empty">Hàng đợi trống</li>';
            } else {
                data.audio_queue.forEach(track => {
                    const li = document.createElement('li');
                    li.className = 'queue-item';
                    
                    const infoDiv = document.createElement('div');
                    infoDiv.className = 'queue-item-info';
                    
                    const nameSpan = document.createElement('span');
                    nameSpan.textContent = track.filename;
                    
                    const tempoSpan = document.createElement('span');
                    tempoSpan.className = 'queue-item-tempo';
                    tempoSpan.textContent = `${Math.round(track.tempo)} BPM`;
                    
                    infoDiv.appendChild(nameSpan);
                    infoDiv.appendChild(tempoSpan);
                    
                    const deleteBtn = document.createElement('span');
                    deleteBtn.className = 'queue-item-delete';
                    deleteBtn.innerHTML = '&times;'; // Ký tự 'X'
                    deleteBtn.dataset.filename = track.filename; 
                    
                    li.appendChild(infoDiv);
                    li.appendChild(deleteBtn);
                    queueList.appendChild(li);
                });
            }

            if (data.server_error) {
                showError(data.server_error);
            }

        } catch (error) {
            showError(error.message);
            syncButton.disabled = true;
            stopButton.disabled = true;
        }
    }

    pollStatus();
    setInterval(pollStatus, 2000); 
});