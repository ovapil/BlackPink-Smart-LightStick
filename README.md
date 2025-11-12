# BlackPink Smart LightStick
**Đồ Án: Hệ Thống Lightstick Đồng Bộ Theo Nhạc Qua Wi-Fi**
Hệ thống sử dụng Server (Python/Flask) để phân tích âm thanh (Nhịp điệu và Cường độ) bằng librosa, sau đó gửi lệnh điều khiển đến nhiều Lightstick (Client) cùng lúc thông qua UDP Multicast trên mạng Wi-Fi.
🌟 Tính Năng Nổi Bật
Đồng bộ Thời gian thực: Đồng bộ 2 (hoặc N) lightstick với độ trễ (Jitter) cực thấp (< 5ms) qua UDP Multicast.
Phân tích Nhạc Thông minh: Server phân tích tệp âm thanh để trích xuất cả Nhịp (Beat) và Cường độ (Intensity), giúp lightstick phản ứng chính xác theo năng lượng của bài hát.
Giao diện Web UI: Giao diện điều khiển (Flask) cho phép chọn chế độ Tĩnh (Static), Nhấp nháy (Blink), hoặc Đồng bộ (Beat Sync) và tải tệp nhạc lên.
Cài đặt Chuyên nghiệp: Tích hợp WiFiManager, cho phép người dùng cuối cài đặt Wi-Fi cho lightstick qua Portal (Cổng Cài Đặt) mà không cần nạp lại code.
Firmware Ổn định (v12.11): Xây dựng trên kiến trúc Máy Trạng Thái (State Machine) 5-state (OFF, LOCAL, WIFI_CONNECTED, SYNC, SETUP) với logic "Fail-Fast" (thất bại nhanh 2s) và tự động kết nối lại (Auto-Reconnect).
Hệ thống Log Phân tích: Giao thức 8-byte tùy chỉnh tích hợp Packet ID và hệ thống LOG,SENT/RECV cho phép đo lường và phân tích chính xác hiệu năng mạng (Mất gói, Jitter).
🏛️ Kiến Trúc Hệ Thống
Hệ thống bao gồm hai thành phần chính: Server (1) và Client (N).
Server (Python):
Sử dụng Flask để tạo Web UI.
Sử dụng librosa để phân tích file nhạc (offline).
Gửi các gói tin UDP 8-byte qua Multicast đến địa chỉ 239.1.1.1.
Client (ESP32):
Sử dụng WiFiManager để kết nối Wi-Fi.
Lắng nghe (subscribe) tại địa chỉ Multicast 239.1.1.1.
Hoạt động dựa trên Máy Trạng Thái (State Machine) 5-state.
Điều khiển LED WS2812 (NeoPixel).
Sơ Đồ Máy Trạng Thái (State Machine v12)
Đây là sơ đồ logic mô tả 5 trạng thái hoạt động của firmware trên ESP32.
