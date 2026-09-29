import time, csv, os, threading, logging, socket, qrcode, glob
import pandas as pd
from PIL import ImageTk, Image
from flask import Flask, render_template_string, jsonify, request, send_file
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog
import sys

# --- [1. 전역 설정] ---
LABELS = {'1': "EAT_OUT", '2': "EAT_MR", '3': "MEET", '4': "W_PC", '5': "W_NPC", '6': "ABS_L", '7': "ABS_S", '8': "OTHERS", '0': "TRANSITION"}
CATEGORIES = {
    "EATING": {"keys": ['1', '2'], "color": "#FF9500"},
    "WORKING": {"keys": ['4', '5'], "color": "#007AFF"},
    "MEETING": {"keys": ['3'], "color": "#AF52DE"},
    "ABSENCE": {"keys": ['6', '7'], "color": "#FF3B30"},
    "OTHERS": {"keys": ['8', '0'], "color": "#8E8E93"}
}

user_states = {} 
recent_logs = []  
lock = threading.Lock()
BASE_LOG_DIR = "GT_LOG"
CONFIG_FILE = "ts_config.txt"
GLOBAL_ROOM = "Lab" 
GROUP_LABELS = ['EAT_OUT', 'EAT_MR', 'MEET']

SYNC_THRESHOLD = 30    
NOISE_FILTER_SEC = 2   

logging.getLogger('werkzeug').setLevel(logging.ERROR)

# --- [2. 핵심 로직] ---
def get_tailscale_ip():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved_ip = f.read().strip()
        if saved_ip:
            msg = f"기억된 IP: {saved_ip}\n접속 주소: http://{saved_ip}:5000\n\n이 주소를 그대로 사용하시겠습니까?"
            if messagebox.askyesno("IP 확인", msg):
                return saved_ip
    new_ip = simpledialog.askstring("새 IP 입력", "새로운 Tailscale IP를 입력하세요:")
    if new_ip:
        with open(CONFIG_FILE, "w") as f: f.write(new_ip)
        return new_ip
    else:
        sys.exit()

def log_event(user_id, room_id, key, action):
    date_str = time.strftime("%Y%m%d")
    daily_dir = os.path.join(BASE_LOG_DIR, date_str)
    os.makedirs(daily_dir, exist_ok=True)
    file_path = os.path.join(daily_dir, f"{user_id}_log.csv")
    file_exists = os.path.exists(file_path)
    now_ts = time.time()
    readable_time = time.strftime("%H:%M:%S", time.localtime(now_ts)) 
    label = LABELS[key]
    log_msg = f"[{readable_time}] {user_id}: {action} {label}"
    with lock:
        with open(file_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Readable_Time", "Action", "Label", "User_ID"])
            writer.writerow([now_ts, readable_time, action, label, user_id])
        recent_logs.append(log_msg)
        if len(recent_logs) > 100: recent_logs.pop(0)
    if user_id not in user_states:
        user_states[user_id] = {'keys': set(), 'last_time': readable_time}
    user_states[user_id]['last_time'] = readable_time
    print(log_msg, flush=True)

def toggle_user_activity(user_id, room_id, key):
    if key not in LABELS: return
    if user_id not in user_states: 
        user_states[user_id] = {'keys': set(), 'last_time': time.strftime("%H:%M:%S")}
    active_keys = user_states[user_id]['keys']
    if key == '0':
        for k in list(active_keys):
            log_event(user_id, room_id, k, "END")
            active_keys.remove(k)
    else:
        if '0' in active_keys:
            log_event(user_id, room_id, '0', "END"); active_keys.remove('0')
        if key in active_keys:
            log_event(user_id, room_id, key, "END"); active_keys.remove(key)
        else:
            log_event(user_id, room_id, key, "START"); active_keys.add(key)

# --- [3. 통합 로직 (3-Tier Output Strategy)] ---

def run_real_merge_task(mode, target_date=None):
    if not os.path.exists(BASE_LOG_DIR): return
    all_folders = sorted([d for d in os.listdir(BASE_LOG_DIR) if os.path.isdir(os.path.join(BASE_LOG_DIR, d))])
    target_folders = []

    if mode == "전체": target_folders = all_folders

    elif mode == "신규 폴더만":
        target_folders = [f for f in all_folders if not os.path.exists(os.path.join(BASE_LOG_DIR, f, f"merged_{f}.csv"))]

    elif mode == "날짜 선택" and target_date:
        if target_date in all_folders: target_folders = [target_date]
        else: print(f"❌ [ERROR] {target_date} 폴더 없음"); return

    for date_str in target_folders:
        folder_path = os.path.join(BASE_LOG_DIR, date_str)
        files = [f for f in glob.glob(os.path.join(folder_path, "*.csv")) if "merged" not in os.path.basename(f) and "timeseries" not in os.path.basename(f)]
        if not files: continue
        all_dfs = [pd.read_csv(f) for f in files]
        raw_df = pd.concat(all_dfs).sort_values('Timestamp')
        raw_df.to_csv(os.path.join(folder_path, f"raw_timeseries_{date_str}.csv"), index=False, encoding='utf-8-sig')
        sessions = []

        for user in raw_df['User_ID'].unique():
            user_df = raw_df[raw_df['User_ID'] == user]
            act_dict = {}

            for _, row in user_df.iterrows():
                lbl, ts, act = row['Label'], row['Timestamp'], row['Action']

                if act == 'START': act_dict[lbl] = ts
                elif act == 'END' and lbl in act_dict:

                    if ts - act_dict[lbl] >= NOISE_FILTER_SEC:
                        sessions.append({'User_ID': user, 'Label': lbl, 'Start': act_dict[lbl], 'End': ts})
                    del act_dict[lbl]
        s_df = pd.DataFrame(sessions)
        if s_df.empty: continue
        final_res = []
        group_df = s_df[s_df['Label'].isin(GROUP_LABELS)].copy()
        solo_df = s_df[~s_df['Label'].isin(GROUP_LABELS)].copy()

        if not group_df.empty:
            for label, g in group_df.groupby('Label'):
                g = g.sort_values('Start')
                while not g.empty:
                    curr = g.iloc[0]
                    overlap = g[(g['Start'] <= curr['End'] + SYNC_THRESHOLD) & (g['End'] >= curr['Start'] - SYNC_THRESHOLD)]
                    final_res.append({'Start': overlap['Start'].min(), 'End': overlap['End'].max(), 'User_ID': ", ".join(sorted(overlap['User_ID'].unique())), 'Label': label, 'Count': len(overlap['User_ID'].unique())})
                    g = g.drop(overlap.index)

        for _, row in solo_df.iterrows():
            final_res.append({'Start': row['Start'], 'End': row['End'], 'User_ID': row['User_ID'], 'Label': row['Label'], 'Count': 1})
        res_df = pd.DataFrame(final_res).sort_values('Start')
        res_df['Start_DT'] = pd.to_datetime(res_df['Start'] + (9*3600), unit='s').dt.strftime('%H:%M:%S')
        res_df['End_DT'] = pd.to_datetime(res_df['End'] + (9*3600), unit='s').dt.strftime('%H:%M:%S')
        res_df[['Start_DT', 'End_DT', 'User_ID', 'Label', 'Count']].to_csv(os.path.join(folder_path, f"merged_{date_str}.csv"), index=False, encoding='utf-8-sig')
        ts_events = []

        for _, r in res_df.iterrows():
            ts_events.append({'Time': r['Start_DT'], 'Action': 'START', 'Label': r['Label'], 'Users': r['User_ID'], 'TS': r['Start']})
            ts_events.append({'Time': r['End_DT'], 'Action': 'END', 'Label': r['Label'], 'Users': r['User_ID'], 'TS': r['End']})
        pd.DataFrame(ts_events).sort_values('TS')[['Time', 'Action', 'Label', 'Users']].to_csv(os.path.join(folder_path, f"timeseries_preprocessed_{date_str}.csv"), index=False, encoding='utf-8-sig')
        print(f"✅ {date_str}: Raw/Preprocessed/Merged 파일 생성 완료!", flush=True)

# --- [4. Flask Server & Web UI] ---
app = Flask(__name__)

STATUS_PAGE_TEMPLATE = """
<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard</title>
<style>
    body { font-family: -apple-system, sans-serif; background: #F2F2F7; padding: 20px; margin: 0; text-align: center; }
    .card { background: white; border-radius: 20px; padding: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); max-width: 600px; margin: 15px auto; }
    h2 { color: #1C1C1E; font-size: 18px; margin-bottom: 15px; display: flex; align-items: center; justify-content: center; }
    .dot { height: 8px; width: 8px; background: #34C759; border-radius: 50%; display: inline-block; margin-right: 8px; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 10px; }
    th { text-align: left; color: #8E8E93; font-size: 11px; padding: 8px; border-bottom: 1px solid #E5E5EA; }
    td { padding: 12px 8px; border-bottom: 1px solid #F2F2F7; font-size: 14px; text-align: left; }
    .active-tag { background: #007AFF; color: white; padding: 3px 7px; border-radius: 5px; font-size: 11px; font-weight: 600; margin: 1px; display: inline-block; }
    .console { background: #1C1C1E; color: #34C759; border-radius: 12px; padding: 15px; font-family: 'Consolas', monospace; font-size: 12px; height: 300px; overflow-y: auto; text-align: left; scroll-behavior: smooth; }
    
    /* [MODIFIED] 간결한 다운로드 버튼 스타일 */
    .btn-download-wrapper { margin-top: 25px; }
    .btn-download { display: block; background: #34C759; color: white; padding: 16px; border-radius: 14px; text-decoration: none; font-size: 17px; font-weight: 700; max-width: 250px; margin: 0 auto; box-shadow: 0 4px 10px rgba(52, 199, 89, 0.2); }
    .btn-sub-text { font-size: 11px; color: #8E8E93; margin-top: 8px; font-weight: 400; }
</style></head>
<body>
<div class="card">
    <h2><span class="dot"></span>Active Status</h2>
    <table><tr><th>User</th><th>Activities</th><th>Updated</th></tr>
    {% for user, info in user_data.items() %}
    <tr><td><strong>{{ user }}</strong></td>
    <td>{% if info['keys'] %}{% for k in info['keys'] %}<span class="active-tag">{{ labels[k] }}</span>{% endfor %}{% else %}<span style="color:#C7C7CC">IDLE</span>{% endif %}</td>
    <td style="color:#8E8E93; font-size:12px;">{{ info['last_time'] }}</td></tr>
    {% endfor %}</table>
</div>
<div class="card" style="background: #1C1C1E;">
    <h2 style="color: white; font-size: 15px;">💻 Live Feed</h2>
    <div id="log-console" class="console">
        {% for log in logs %}<div>> {{ log }}</div>{% endfor %}
    </div>
</div>
<div class="btn-download-wrapper">
    <a href="/download_today" class="btn-download">Download</a>
    <div class="btn-sub-text">Get Today's Raw Log (CSV)</div>
</div>
<p style="text-align:center; color:gray; font-size:11px; margin-top:30px;">Refreshes every 5s</p>
<script>
    window.onload = function() {
        var consoleBox = document.getElementById("log-console");
        consoleBox.scrollTop = consoleBox.scrollHeight;
    };
</script>
</body></html>"""

@app.route('/')
def index(): return render_template_string("""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>:root { --ios-bg: #F2F2F7; --ios-blue: #007AFF; } body { font-family: -apple-system, sans-serif; background: var(--ios-bg); margin: 0; padding: 20px; text-align: center; }
.card { background: white; padding: 25px; border-radius: 20px; margin: 15px auto; max-width: 400px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
input { width: 90%; padding: 15px; border: 1px solid #ddd; border-radius: 12px; font-size: 17px; margin-bottom: 15px; box-sizing: border-box; }
.btn-blue { background: var(--ios-blue); color: white; border: none; width: 100%; padding: 16px; border-radius: 14px; font-size: 17px; font-weight: 700; cursor: pointer; }
.category-title { text-align: left; font-size: 13px; font-weight: 600; color: #8E8E93; margin: 20px 0 8px 10px; text-transform: uppercase; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.act-btn { height: 65px; border: none; border-radius: 15px; background: white; font-weight: 600; font-size: 15px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); cursor: pointer; }
.act-btn.active { background: var(--cat-color) !important; color: white !important; } .hidden { display: none !important; }</style></head>
<body><div id="login-view" class="card"><h2>GT Logger</h2><input type="text" id="user-name" placeholder="English Name"><button class="btn-blue" onclick="doLogin()">Start</button></div>
<div id="main-view" class="hidden"><div class="card"><div id="display-user" style="font-weight:bold"></div><div id="status-display" style="font-size: 20px; font-weight: 800; color: #007AFF; margin-top:5px;">IDLE</div></div>
{% for cat, data in categories.items() %}<div class="category-title">{{cat}}</div><div class="grid">{% for k in data['keys'] %}
<button id="btn-{{k}}" class="act-btn" onclick="handleClick('{{k}}')" style="--cat-color: {{data['color']}}">{{labels[k]}}</button>{% endfor %}</div>{% endfor %}
<button onclick="window.open('/status_view', '_blank')" style="margin-top:20px; border:none; background:none; color:#007AFF; font-size:14px; text-decoration:underline;">실시간 로그 보기</button><br>
<button onclick="location.reload()" style="margin-top:20px; border:none; background:none; color:gray; font-size:14px;">로그아웃</button></div>
<script>let user = ""; function doLogin(){ user = document.getElementById('user-name').value.trim(); if(!user) return; fetch(`/login_log?user=${user}`);
document.getElementById('login-view').classList.add('hidden'); document.getElementById('main-view').classList.remove('hidden'); document.getElementById('display-user').innerText = user; sync(); }
function sync() { if(user) fetch(`/status?user=${user}`).then(r => r.json()).then(d => updateUI(d)); }
function handleClick(k) { fetch(`/log?user=${user}&key=${k}`).then(r => r.json()).then(d => updateUI(d)); }
function updateUI(d) { document.getElementById('status-display').innerText = d.list.join(', ') || 'IDLE';
document.querySelectorAll('.act-btn').forEach(b => b.classList.remove('active')); if(d.keys) d.keys.forEach(k => { const btn = document.getElementById('btn-'+k); if(btn) btn.classList.add('active'); }); }
window.onfocus = sync;</script></body></html>""", categories=CATEGORIES, labels=LABELS)

@app.route('/status_view')
def status_view(): return render_template_string(STATUS_PAGE_TEMPLATE, user_data=user_states, labels=LABELS, logs=recent_logs)

@app.route('/download_today')
def download_today():
    date_str = time.strftime("%Y%m%d")
    file_path = os.path.join(BASE_LOG_DIR, date_str, f"raw_timeseries_{date_str}.csv")
    if os.path.exists(file_path): return send_file(file_path, as_attachment=True)
    else: return "<script>alert('통합 데이터가 아직 생성되지 않았습니다. 메인 PC에서 Integrate 버튼을 먼저 눌러주세요!'); history.back();</script>"

@app.route('/login_log')
def login_log():
    u = request.args.get('user')
    if u not in user_states: user_states[u] = {'keys': set(), 'last_time': time.strftime("%H:%M:%S")}
    print(f"[{time.strftime('%H:%M:%S')}] {u}: LOGIN", flush=True); return "OK"

@app.route('/status')
def get_status(): u = request.args.get('user'); active = user_states.get(u, {}).get('keys', set()); return jsonify(list=[LABELS[k] for k in active], keys=list(active))

@app.route('/log')
def web_log():
    u = request.args.get('user'); k = request.args.get('key')
    toggle_user_activity(u, GLOBAL_ROOM, k)
    active = user_states.get(u, {}).get('keys', set()); return jsonify(list=[LABELS[k] for k in active], keys=list(active))

class GTDashboard:
    def __init__(self, root, server_ip):
        self.root = root; self.server_ip = server_ip; self.setup_ui()
    def setup_ui(self):
        self.root.title("NRF Manager"); 
        self.root.geometry("640x820"); 
        self.root.configure(bg="#F2F2F7")

        header = tk.Frame(self.root, bg="#F2F2F7"); header.pack(pady=(60, 30))

        tk.Label(header, text="NRF Data Logger", font=("Helvetica Neue", 38, "bold"), bg="#F2F2F7", fg="#1C1C1E").pack()
        tk.Label(header, text=f"IIS Lab | http://{self.server_ip}:5000", font=("Helvetica Neue", 12), bg="#F2F2F7", fg="#8E8E93").pack(pady=5)

        card = tk.Frame(self.root, bg="#FFFFFF", padx=30, pady=30); card.pack(pady=10, padx=50, fill="x")

        self.btn_server = tk.Button(card, text="Start Server", command=self.start_server, bg="#007AFF", fg="white", font=("Helvetica Neue", 12, "bold"), height=2, relief="flat", cursor="hand2")
        self.btn_server.pack(fill="x", pady=(10, 20))

        tk.Label(card, text="INTEGRATION MODE", font=("Helvetica Neue", 9, "bold"), bg="#FFFFFF", fg="#8E8E93").pack(anchor="w")
        
        self.mode_var = tk.StringVar(self.root); self.mode_var.set("신규 폴더만")
        opt = tk.OptionMenu(card, self.mode_var, "전체", "신규 폴더만", "날짜 선택"); opt.config(bg="#F2F2F7", fg="#007AFF", font=("Helvetica Neue", 11, "bold"), relief="flat", highlightthickness=0); opt.pack(fill="x", pady=10)
        
        self.btn_merge = tk.Button(card, text="Integrate Data", command=self.start_merge, bg="#34C759", fg="white", font=("Helvetica Neue", 12, "bold"), height=2, relief="flat", cursor="hand2"); self.btn_merge.pack(fill="x", pady=10)
        self.btn_qr = tk.Button(card, text="Show QR Code", command=self.show_qr_popup, bg="#AF52DE", fg="white", font=("Helvetica Neue", 12, "bold"), height=2, relief="flat"); self.btn_qr.pack(fill="x")
        
        console = tk.Frame(self.root, bg="#F2F2F7"); console.pack(fill="both", expand=True, padx=40, pady=(20, 40))
        self.log_display = scrolledtext.ScrolledText(console, bg="#FFFFFF", font=("Consolas", 10), relief="flat", padx=15, pady=15); 
        self.log_display.pack(fill="both", expand=True); 
        sys.stdout = self

    def write(self, text): self.log_display.config(state="normal"); self.log_display.insert(tk.END, text); self.log_display.see(tk.END); self.log_display.config(state="disabled")

    def flush(self): pass

    def start_server(self):
        self.btn_server.config(state="disabled", text="● Online", bg="#E5E5EA", fg="#8E8E93")
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()

        print(f"🚀 [SYSTEM] 서버 가동 중.. 로그 확인 : http://{self.server_ip}:5000/status_view")


    def start_merge(self):
        m = self.mode_var.get(); t = simpledialog.askstring("날짜 선택", "YYYYMMDD:") if m == "날짜 선택" else None
        print(f"🔄 [PROCESS] Integrate & Refine 시작"); threading.Thread(target=lambda: run_real_merge_task(m, t), daemon=True).start()


    def show_qr_popup(self):
        qr_window = tk.Toplevel(self.root); qr_window.title("Scan to Connect"); qr_window.geometry("350x450"); qr_window.configure(bg="white")
        url = f"http://{self.server_ip}:5000"
        qr = qrcode.QRCode(box_size=10, border=2); qr.add_data(url); qr.make(fit=True)

        self.tk_qr_img = ImageTk.PhotoImage(qr.make_image(fill_color="black", back_color="white"))
        tk.Label(qr_window, text="Mobile Access QR", font=("Helvetica Neue", 12, "bold"), bg="white").pack(pady=20); tk.Label(qr_window, image=self.tk_qr_img, bg="white").pack(); tk.Label(qr_window, text=url, font=("Helvetica Neue", 10), bg="white", fg="#8E8E93").pack(pady=10); tk.Button(qr_window, text="Close", command=qr_window.destroy, bg="#007AFF", fg="white", relief="flat", padx=20).pack(pady=10)

if __name__ == "__main__":
    ts_ip = get_tailscale_ip()
    if not ts_ip: sys.exit()
    root = tk.Tk(); app_gui = GTDashboard(root, ts_ip); root.mainloop()