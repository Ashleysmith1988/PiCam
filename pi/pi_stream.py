#!/usr/bin/env python3
"""
PiCam: Pi Zero W camera stream with a phone-friendly Wi-Fi settings page.

Pages (open on phone / computer / headset browser):
    /           normal view
    /vr         side-by-side view for Gear VR / Cardboard
    /settings   switch between home Wi-Fi and the PiCam hotspot, join networks

Addresses:
    Home Wi-Fi:   http://picam.local:8000/
    PiCam hotspot: http://10.42.0.1:8000/

At startup, if no Wi-Fi is connected within BOOT_WAIT seconds, the Pi starts
its own hotspot automatically.
"""

import hashlib
import io
import json
import logging
import os
import socketserver
import subprocess
import threading
import time
import urllib.request
from http import server
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder, Quality
from picamera2.outputs import FileOutput

# ---- Camera settings (lower these if the stream lags) ----------------------
WIDTH, HEIGHT = 640, 480
FRAMERATE = 24
PORT = 8000
QUALITY = Quality.MEDIUM     # LOW / MEDIUM / HIGH / VERY_HIGH
FLIP = True                  # rotate 180 degrees
ZOOM_MAX = 4.0               # highest binocular zoom (sensor crop)

# ---- Network settings -------------------------------------------------------
HOTSPOT_CON = "Hotspot"      # NetworkManager connection name
HOTSPOT_SSID = "PiCam"
HOTSPOT_PASS = "picam1234"   # at least 8 characters
BOOT_WAIT = 120              # seconds to wait for home Wi-Fi before hotspot

# ---- Self-update from GitHub -------------------------------------------------
GITHUB_REPO = "Ashleysmith1988/PiCam"
GITHUB_FILE = "pi/pi_stream.py"     # where this script lives in the repository
SCRIPT_PATH = os.path.abspath(__file__)
# -----------------------------------------------------------------------------


# ============================ Network helpers ================================
net_lock = threading.Lock()


def nm(*args, timeout=30):
    """Run nmcli (via sudo if not root). Returns (code, stdout, stderr)."""
    cmd = ["nmcli", *args]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"


def current_connection():
    _, out, _ = nm("-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0")
    return out.split(":", 1)[1].strip() if ":" in out else ""


def hotspot_exists():
    _, out, _ = nm("-t", "-f", "NAME", "connection", "show")
    return HOTSPOT_CON in out.splitlines()


def saved_home_networks():
    _, out, _ = nm("-t", "-f", "NAME,TYPE", "connection", "show")
    names = []
    for line in out.splitlines():
        name, _, typ = line.rpartition(":")
        if typ == "802-11-wireless" and name != HOTSPOT_CON:
            names.append(name.replace("\\:", ":"))
    return names


def start_hotspot():
    if hotspot_exists():
        code, _, err = nm("connection", "up", HOTSPOT_CON)
    else:
        code, _, err = nm("device", "wifi", "hotspot", "ifname", "wlan0",
                          "ssid", HOTSPOT_SSID, "password", HOTSPOT_PASS)
    # Hotspot is only ever started on purpose, never automatically by NM
    nm("connection", "modify", HOTSPOT_CON, "connection.autoconnect", "no")
    if code == 0:
        logging.info("Hotspot on")
    else:
        logging.info("Hotspot FAILED to start: %s", err)
    return code == 0


def go_home():
    nm("connection", "down", HOTSPOT_CON)
    time.sleep(2)
    for name in saved_home_networks():
        code, _, _ = nm("connection", "up", name, timeout=45)
        if code == 0:
            logging.info("Connected to home Wi-Fi '%s'", name)
            return True
    logging.info("Home Wi-Fi not found - back to hotspot")
    start_hotspot()
    return False


def join_network(ssid, password):
    already_saved = ssid in saved_home_networks()
    nm("connection", "down", HOTSPOT_CON)
    time.sleep(2)
    nm("device", "wifi", "rescan")
    time.sleep(4)
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    code, _, err = nm(*args, timeout=60)
    if code != 0:
        logging.info("Joining '%s' failed: %s", ssid, err)
        if not already_saved:
            nm("connection", "delete", ssid)   # remove the broken profile
        start_hotspot()
        return False
    logging.info("Joined '%s'", ssid)
    return True


def scan_networks():
    _, out, _ = nm("-t", "-f", "SSID,SIGNAL,SECURITY",
                   "device", "wifi", "list", "--rescan", "auto")
    nets = {}
    for line in out.splitlines():
        parts = line.replace("\\:", "\0").split(":")
        if len(parts) < 3:
            continue
        ssid = parts[0].replace("\0", ":")
        if not ssid or ssid == HOTSPOT_SSID:
            continue
        try:
            sig = int(parts[1])
        except ValueError:
            sig = 0
        if ssid not in nets or sig > nets[ssid]["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig,
                          "secure": parts[2] not in ("", "--")}
    return sorted(nets.values(), key=lambda n: -n["signal"])


def net_status():
    con = current_connection()
    mode = "hotspot" if con == HOTSPOT_CON else ("home" if con else "none")
    ips = subprocess.run(["hostname", "-I"], capture_output=True,
                         text=True).stdout.split()
    return {"mode": mode, "network": con, "ip": ips[0] if ips else "",
            "saved": saved_home_networks(), "hotspot_ssid": HOTSPOT_SSID}


# ================================ Zoom =======================================
camera = None          # set at start-up
zoom_level = 1.0


def pick_sensor_mode(cam):
    """Choose the most detailed sensor mode that still sees the full view
    and keeps up with FRAMERATE, so zooming in shows real extra detail."""
    try:
        modes = cam.sensor_modes
        full = max(m["crop_limits"][2] * m["crop_limits"][3] for m in modes)
        ok = [m for m in modes
              if m["crop_limits"][2] * m["crop_limits"][3] >= 0.95 * full
              and m.get("fps", 0) >= FRAMERATE]
        if not ok:
            return {}
        best = max(ok, key=lambda m: m["size"][0] * m["size"][1])
        logging.info("Sensor mode %s @ %.0f fps", best["size"], best.get("fps", 0))
        return {"output_size": best["size"], "bit_depth": best["bit_depth"]}
    except Exception as e:
        logging.info("Using default sensor mode (%s)", e)
        return {}


def set_zoom(z):
    """Crop the middle of the sensor. 1.0 = full view."""
    global zoom_level
    z = max(1.0, min(ZOOM_MAX, float(z)))
    try:
        _, (x0, y0, w0, h0), _ = camera.camera_controls["ScalerCrop"]
        ratio = WIDTH / HEIGHT                      # keep the picture 4:3
        if w0 / h0 > ratio:
            nw = int(h0 * ratio); x0 += (w0 - nw) // 2; w0 = nw
        else:
            nh = int(w0 / ratio); y0 += (h0 - nh) // 2; h0 = nh
        w, h = int(w0 / z) & ~1, int(h0 / z) & ~1
        x, y = x0 + (w0 - w) // 2, y0 + (h0 - h) // 2
        camera.set_controls({"ScalerCrop": (x, y, w, h)})
        zoom_level = z
    except Exception as e:
        logging.info("Zoom failed: %s", e)
    return zoom_level


# ============================== Self-update ==================================
def _short_hash(data):
    # Line endings are ignored so a Windows/Linux difference isn't "an update"
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:10]


def local_version():
    with open(SCRIPT_PATH, "rb") as f:
        return _short_hash(f.read())


def fetch_latest_script():
    url = ("https://api.github.com/repos/%s/contents/%s?ref=main"
           % (GITHUB_REPO, GITHUB_FILE))
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw",
        "User-Agent": "PiCam-Pi",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def check_update():
    try:
        data = fetch_latest_script()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": "No pi/pi_stream.py found in the GitHub repository"}
        return {"ok": False, "error": "GitHub replied %d" % e.code}
    except Exception:
        return {"ok": False, "error": "Can't reach GitHub - the Pi needs to be on home Wi-Fi"}
    latest = _short_hash(data)
    current = local_version()
    return {"ok": True, "current": current, "latest": latest,
            "available": latest != current}


def install_update():
    data = fetch_latest_script()
    # Safety checks before replacing the running script
    compile(data, "pi_stream.py", "exec")          # raises SyntaxError if broken
    if b"StreamingHandler" not in data:
        raise ValueError("that file doesn't look like the PiCam script")
    with open(SCRIPT_PATH, "rb") as f:
        old = f.read()
    with open(SCRIPT_PATH + ".bak", "wb") as f:   # keep the previous version
        f.write(old)
    tmp = SCRIPT_PATH + ".new"
    with open(tmp, "wb") as f:
        f.write(data.replace(b"\r\n", b"\n"))
    os.replace(tmp, SCRIPT_PATH)
    logging.info("Updated to %s - restarting", _short_hash(data))

    def restart():
        time.sleep(2)
        cmd = ["systemctl", "restart", "picam"]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        subprocess.Popen(cmd)
    threading.Thread(target=restart, daemon=True).start()
    return _short_hash(data)


def run_later(fn, *args):
    """Run a network change after the HTTP reply has been sent."""
    def worker():
        time.sleep(1.5)
        with net_lock:
            fn(*args)
    threading.Thread(target=worker, daemon=True).start()


def boot_fallback():
    if hotspot_exists():
        nm("connection", "modify", HOTSPOT_CON, "connection.autoconnect", "no")
    for _ in range(BOOT_WAIT):
        if current_connection():
            return
        time.sleep(1)
    logging.info("No Wi-Fi after %ds - starting hotspot", BOOT_WAIT)
    with net_lock:
        start_hotspot()


# ================================ Pages ======================================
NORMAL_PAGE = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiCam - Tiger</title>
<style>
  html,body{margin:0;height:100%;background:#000;overflow:hidden;
            display:flex;align-items:center;justify-content:center;
            font-family:"Courier New",monospace;-webkit-user-select:none;user-select:none}
  /* Stage keeps the camera's 4:3 shape; every overlay sits inside it */
  #stage{position:relative;width:100vw;height:75vw;overflow:hidden}
  #stage img{position:absolute;top:0;left:0;width:100%;height:100%}
  .layer{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
  .hidden{display:none!important}

  /* ---- Old film ---- */
  body.film #cam{filter:sepia(.45) contrast(1.15) saturate(.7) brightness(.95)}
  #vignette{background:radial-gradient(ellipse at center,transparent 55%,rgba(0,0,0,.75) 100%)}
  #grain{position:absolute;top:0;left:0;width:100%;height:100%;opacity:.16;mix-blend-mode:overlay}

  /* ---- Driver's vision slit: armour plate with a narrow glass block ---- */
  #slit{position:absolute;left:10%;right:10%;top:33%;height:28%;border-radius:6px;
        background:linear-gradient(rgba(70,90,60,.18),rgba(40,55,35,.28));
        box-shadow:0 0 0 200vmax #15150f,
                   inset 0 0 0 3px #3a3a30, inset 0 0 0 6px #0c0c08,
                   inset 0 10px 25px rgba(0,0,0,.7), inset 0 -6px 18px rgba(0,0,0,.6)}
  #plate{background:radial-gradient(circle,#55554a 0 3px,#22221b 4px,transparent 5px) 0 0/64px 64px;
         -webkit-mask:linear-gradient(#000 0 31%,transparent 31% 63%,#000 63%);
                 mask:linear-gradient(#000 0 31%,transparent 31% 63%,#000 63%);opacity:.7}
  #slitlabel{position:absolute;left:0;right:0;top:65%;text-align:center;
             color:#8a8a70;font-size:16px;letter-spacing:.3em}

  /* ---- Gunner's sight: round field of view + reticle ---- */
  #scope{background:radial-gradient(circle at center,transparent 0 41%,#050505 42%)}
  #reticle{width:100%;height:100%}

  /* ---- Binoculars: two eyepieces, 1.6x zoom ---- */
  #zoombar{position:fixed;right:14px;top:50%;margin-top:-95px;z-index:10;
           display:flex;flex-direction:column;align-items:center;transition:opacity .4s}
  #zoombar button{width:64px;height:64px;font:bold 32px sans-serif;color:#eee;
           background:#000a;border:1px solid #888;border-radius:50%}
  #zoombar button:active{background:#7a6a2a}
  #zoomval{color:#e8e2c0;font:bold 18px "Courier New",monospace;margin:12px 0;
           text-shadow:0 0 3px #000}
  #zoombar.faded{opacity:0;pointer-events:none}

  /* ---- HUD ---- */
  .hud{position:absolute;color:#e8e2c0;font-size:18px;
       text-shadow:0 0 3px #000,0 0 2px #000;letter-spacing:.08em}
  #h-title{top:3%;left:0;right:0;text-align:center;font-weight:bold}
  #h-rec{bottom:3%;right:3%}
  #recdot{color:#e03030;animation:blink 1s steps(2) infinite}
  @keyframes blink{50%{opacity:0}}
  #h-link{bottom:3%;left:3%}
  .bar{display:inline-block;width:.45em;margin-right:2px;background:#e8e2c0;
       vertical-align:bottom;opacity:.25}
  .bar.on{opacity:1}

  /* ---- Controls (fade out after a few seconds; tap to show) ---- */
  #controls{position:fixed;bottom:10px;left:50%;transform:translateX(-50%);
            display:flex;gap:6px;transition:opacity .4s;z-index:10}
  #controls button{font:bold 14px sans-serif;color:#ddd;background:#000a;
            border:1px solid #666;border-radius:6px;padding:8px 12px}
  #controls button.on{background:#7a6a2a;border-color:#c9b25a;color:#fff}
  #controls.faded{opacity:0;pointer-events:none}
  nav{position:fixed;top:8px;right:8px;display:flex;gap:6px;z-index:10;transition:opacity .4s}
  nav a{color:#fff;font:16px sans-serif;background:#0008;padding:6px 10px;
        border-radius:6px;text-decoration:none}
  nav.faded{opacity:0;pointer-events:none}
</style></head>
<body>
<div id="stage">
  <img id="cam" src="/stream.mjpg">

  <div id="driver" class="layer">
    <div id="plate" class="layer"></div>
    <div id="slit"></div>
    <div id="slitlabel">FAHRERSEHKLAPPE</div>
  </div>

  <div id="gunner" class="layer">
    <div id="scope" class="layer"></div>
    <svg id="reticle" class="layer" viewBox="0 0 400 300" preserveAspectRatio="xMidYMid meet">
      <g fill="none" stroke="#000" stroke-width="2.4" stroke-linejoin="miter">
        <path d="M188 176 L200 150 L212 176"/>
        <path d="M154 176 L160 163 L166 176"/><path d="M234 176 L240 163 L246 176"/>
        <path d="M114 176 L120 163 L126 176"/><path d="M274 176 L280 163 L286 176"/>
        <line x1="200" y1="186" x2="200" y2="232"/>
      </g>
      <g fill="none" stroke="#000" stroke-width="1.6">
        <line x1="165" y1="70" x2="165" y2="130"/>
        <line x1="159" y1="70" x2="171" y2="70"/><line x1="161" y1="85" x2="169" y2="85"/>
        <line x1="159" y1="100" x2="171" y2="100"/><line x1="161" y1="115" x2="169" y2="115"/>
        <line x1="159" y1="130" x2="171" y2="130"/>
      </g>
      <g font-family="Courier New,monospace" font-size="9" fill="#000">
        <text x="175" y="73">20</text><text x="175" y="103">10</text><text x="175" y="133">0</text>
        <text x="180" y="250">TZF 9b</text>
      </g>
    </svg>
  </div>

  <div id="binos" class="layer">
    <svg class="layer" viewBox="0 0 400 300" preserveAspectRatio="none">
      <defs>
        <filter id="soft" x="-10%" y="-10%" width="120%" height="120%">
          <feGaussianBlur stdDeviation="6"/>
        </filter>
        <mask id="eyes">
          <rect width="400" height="300" fill="#fff"/>
          <g filter="url(#soft)">
            <circle cx="148" cy="150" r="100" fill="#000"/>
            <circle cx="252" cy="150" r="100" fill="#000"/>
          </g>
        </mask>
      </defs>
      <rect width="400" height="300" fill="#060606" mask="url(#eyes)"/>
    </svg>
    <svg class="layer" viewBox="0 0 400 300" preserveAspectRatio="xMidYMid meet">
      <g stroke="#000" stroke-width="1.6" fill="none">
        <line x1="130" y1="150" x2="270" y2="150"/>
        <line x1="130" y1="145" x2="130" y2="155"/><line x1="144" y1="147" x2="144" y2="153"/>
        <line x1="158" y1="145" x2="158" y2="155"/><line x1="172" y1="147" x2="172" y2="153"/>
        <line x1="186" y1="145" x2="186" y2="155"/>
        <line x1="214" y1="145" x2="214" y2="155"/><line x1="228" y1="147" x2="228" y2="153"/>
        <line x1="242" y1="145" x2="242" y2="155"/><line x1="256" y1="147" x2="256" y2="153"/>
        <line x1="270" y1="145" x2="270" y2="155"/>
        <path d="M193 150 L200 140 L207 150" stroke-width="2"/>
        <line x1="200" y1="160" x2="200" y2="200"/>
        <line x1="195" y1="170" x2="205" y2="170"/><line x1="195" y1="180" x2="205" y2="180"/>
        <line x1="195" y1="190" x2="205" y2="190"/>
      </g>
      <text x="200" y="268" text-anchor="middle" font-family="Courier New,monospace"
            font-size="10" fill="#9a9478">DF 6x30</text>
    </svg>
  </div>

  <div id="film" class="layer">
    <canvas id="grain" width="128" height="96"></canvas>
    <div id="vignette" class="layer"></div>
  </div>

  <div id="hud" class="layer">
    <div id="h-title" class="hud">PANZERKAMPFWAGEN VI &middot; TIGER</div>
    <div id="h-link" class="hud">LINK <span id="bars"></span> <span id="lat">--</span></div>
    <div id="h-rec" class="hud"><span id="recdot">&#9679;</span> REC <span id="clock"></span></div>
  </div>
</div>

<nav id="nav"><a href="/vr">VR mode</a><a href="/settings">Settings</a></nav>
<div id="zoombar" class="hidden">
  <button id="zin">+</button>
  <div id="zoomval">1.0x</div>
  <button id="zout">&minus;</button>
</div>
<div id="controls">
  <button data-view="driver">DRIVER</button>
  <button data-view="gunner">GUNNER</button>
  <button data-view="binos">BINOS</button>
  <button data-view="clear">CLEAR</button>
  <button data-toggle="hud">HUD</button>
  <button data-toggle="film">FILM</button>
</div>

<script>
  // ---- Keep the picture 4:3 and as large as the screen allows ----
  var stage = document.getElementById('stage');
  function fit(){
    var w = window.innerWidth, h = window.innerHeight;
    if (w / h > 4 / 3) w = h * 4 / 3; else h = w * 3 / 4;
    stage.style.width = Math.round(w) + 'px';
    stage.style.height = Math.round(h) + 'px';
    var f = Math.max(11, Math.round(h * 0.032));
    var hud = document.querySelectorAll('.hud');
    for (var i = 0; i < hud.length; i++) hud[i].style.fontSize = f + 'px';
    document.getElementById('slitlabel').style.fontSize = Math.round(f * 0.9) + 'px';
  }
  fit(); window.addEventListener('resize', fit);

  // ---- Binocular zoom (done on the Pi by cropping the camera sensor) ----
  var ZOOM_STEPS = [1, 1.25, 1.5, 2, 2.5, 3, 4];
  function sendZoom(z){
    document.getElementById('zoomval').textContent = z.toFixed(1) + 'x';
    var x = new XMLHttpRequest();
    x.open('POST', '/api/zoom');
    x.setRequestHeader('Content-Type', 'application/json');
    x.onload = function(){
      try {
        var r = JSON.parse(x.responseText);
        document.getElementById('zoomval').textContent = r.zoom.toFixed(1) + 'x';
      } catch(e) {}
    };
    x.send(JSON.stringify({zoom: z}));
  }
  function stepZoom(dir){
    var cur = state.zoom || 1.5, next = cur, i;
    if (dir > 0) {
      for (i = 0; i < ZOOM_STEPS.length; i++) if (ZOOM_STEPS[i] > cur) { next = ZOOM_STEPS[i]; break; }
    } else {
      for (i = ZOOM_STEPS.length - 1; i >= 0; i--) if (ZOOM_STEPS[i] < cur) { next = ZOOM_STEPS[i]; break; }
    }
    state.zoom = next;
    save(); sendZoom(state.zoom); wake();
  }

  // ---- Saved settings (kept per device; falls back to defaults) ----
  var state = {view:'driver', hud:true, film:true};
  try { var s = JSON.parse(localStorage.getItem('picamOverlay')); if (s) state = s; } catch(e) {}
  function save(){ try { localStorage.setItem('picamOverlay', JSON.stringify(state)); } catch(e) {} }

  function apply(){
    document.getElementById('driver').classList.toggle('hidden', state.view !== 'driver');
    document.getElementById('gunner').classList.toggle('hidden', state.view !== 'gunner');
    document.getElementById('binos').classList.toggle('hidden', state.view !== 'binos');
    document.getElementById('zoombar').classList.toggle('hidden', state.view !== 'binos');
    // Binoculars use the saved zoom; every other view goes back to the full picture
    sendZoom(state.view === 'binos' ? (state.zoom || 1.5) : 1);
    document.getElementById('hud').classList.toggle('hidden', !state.hud);
    document.getElementById('film').classList.toggle('hidden', !state.film);
    document.body.classList.toggle('film', state.film);
    Array.prototype.forEach.call(document.querySelectorAll('#controls button'), function(b){
      var on = b.dataset.view ? b.dataset.view === state.view : state[b.dataset.toggle];
      b.classList.toggle('on', !!on);
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll('#controls button'), function(b){
    b.addEventListener('click', function(ev){
      ev.stopPropagation();
      if (b.dataset.view) state.view = b.dataset.view;
      else state[b.dataset.toggle] = !state[b.dataset.toggle];
      save(); apply(); wake();
    });
  });
  apply();
  document.getElementById('zin').addEventListener('click', function(ev){ ev.stopPropagation(); stepZoom(1); });
  document.getElementById('zout').addEventListener('click', function(ev){ ev.stopPropagation(); stepZoom(-1); });

  // ---- Controls fade after 4 s; tap anywhere to bring them back ----
  var fadeTimer;
  function wake(){
    document.getElementById('controls').classList.remove('faded');
    document.getElementById('nav').classList.remove('faded');
    document.getElementById('zoombar').classList.remove('faded');
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(function(){
      document.getElementById('controls').classList.add('faded');
      document.getElementById('nav').classList.add('faded');
      document.getElementById('zoombar').classList.add('faded');
    }, 4000);
  }
  document.body.addEventListener('click', wake);
  wake();

  // ---- Film grain (drawn on the viewing device, not the Pi) ----
  var g = document.getElementById('grain'), gx = g.getContext('2d');
  var img = gx.createImageData(g.width, g.height);
  setInterval(function(){
    if (!state.film) return;
    var d = img.data;
    for (var i = 0; i < d.length; i += 4){
      var v = Math.random() * 255 | 0;
      d[i] = d[i+1] = d[i+2] = v; d[i+3] = 255;
    }
    gx.putImageData(img, 0, 0);
  }, 80);

  // ---- HUD clock ----
  function pad(n){ return (n < 10 ? '0' : '') + n; }
  function tick(){
    var t = new Date();
    document.getElementById('clock').textContent =
      pad(t.getHours()) + ':' + pad(t.getMinutes()) + ':' + pad(t.getSeconds());
  }
  tick(); setInterval(tick, 1000);

  // ---- HUD link strength: round-trip time to the Pi ----
  var barsEl = document.getElementById('bars');
  for (var i = 1; i <= 4; i++){
    var b = document.createElement('span'); b.className = 'bar';
    b.style.height = (0.3 * i) + 'em'; barsEl.appendChild(b);
  }
  function ping(){
    var t0 = Date.now();
    var x = new XMLHttpRequest();
    x.open('GET', '/api/ping?' + t0); x.timeout = 4000;
    x.onload = function(){
      var ms = Date.now() - t0;
      var n = ms < 60 ? 4 : ms < 150 ? 3 : ms < 400 ? 2 : 1;
      Array.prototype.forEach.call(barsEl.childNodes, function(b, i){ b.classList.toggle('on', i < n); });
      document.getElementById('lat').textContent = ms + 'ms';
    };
    x.onerror = x.ontimeout = function(){
      Array.prototype.forEach.call(barsEl.childNodes, function(b){ b.classList.remove('on'); });
      document.getElementById('lat').textContent = 'LOST';
    };
    x.send();
  }
  ping(); setInterval(ping, 3000);
</script>
</body></html>
"""

VR_PAGE = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiCam VR</title>
<style>
  html,body{margin:0;height:100%;background:#000;overflow:hidden}
  #wrap{display:flex;width:100%;height:100%}
  .eye{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
  .eye img,.eye canvas{max-width:100%;max-height:100%}
  #hint{position:fixed;bottom:10px;width:100%;text-align:center;
        color:#aaa;font:14px sans-serif}
</style></head>
<body>
<div id="wrap">
  <div class="eye"><img id="src" src="/stream.mjpg"></div>
  <div class="eye"><canvas id="right"></canvas></div>
</div>
<div id="hint">Rotate to landscape, then tap to go full screen</div>
<script>
  const img = document.getElementById('src');
  const cv  = document.getElementById('right');
  const ctx = cv.getContext('2d');
  function draw(){
    if (img.naturalWidth){
      if (cv.width !== img.naturalWidth){
        cv.width = img.naturalWidth; cv.height = img.naturalHeight;
      }
      try { ctx.drawImage(img, 0, 0); } catch(e) {}
    }
    requestAnimationFrame(draw);
  }
  draw();
  document.body.addEventListener('click', () => {
    const el = document.documentElement;
    (el.requestFullscreen || el.webkitRequestFullscreen || (()=>{})).call(el);
    if (screen.orientation && screen.orientation.lock)
      screen.orientation.lock('landscape').catch(()=>{});
    document.getElementById('hint').style.display = 'none';
  });
</script>
</body></html>
"""

SETTINGS_PAGE = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiCam Settings</title>
<style>
  body{margin:0;background:#111;color:#eee;font:16px sans-serif}
  main{max-width:480px;margin:auto;padding:16px}
  h1{font-size:22px;margin:4px 0 16px}
  h2{font-size:17px;margin:24px 0 8px;color:#aaa}
  .card{background:#1d1d1d;border-radius:10px;padding:14px}
  .mode{font-size:20px;font-weight:bold}
  .hotspot{color:#f0a030}.home{color:#4cc46a}.none{color:#e55}
  button{width:100%;padding:14px;margin-top:10px;font-size:16px;border:0;
         border-radius:8px;background:#2b6cdf;color:#fff}
  button.alt{background:#333}
  button:disabled{opacity:.4}
  input{width:100%;box-sizing:border-box;padding:12px;margin-top:8px;font-size:16px;
        border-radius:8px;border:1px solid #444;background:#000;color:#eee}
  .net{display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid #333;
       cursor:pointer}
  .net:last-child{border:0}
  #msg{display:none;margin-top:16px;padding:14px;border-radius:10px;background:#263a26;
       line-height:1.4}
  a{color:#7ab0ff}
  .small{color:#999;font-size:14px}
</style></head>
<body><main>
<h1>PiCam Settings</h1>
<div class="card">
  <div class="small">Currently</div>
  <div id="mode" class="mode">...</div>
  <div id="detail" class="small"></div>
  <button id="bHot" onclick="setMode('hotspot')">Use PiCam hotspot</button>
  <button id="bHome" class="alt" onclick="setMode('home')">Use home Wi-Fi</button>
</div>

<h2>Join a Wi-Fi network</h2>
<div class="card">
  <div id="nets" class="small">Tap Scan to look for networks</div>
  <button class="alt" onclick="scan()">Scan</button>
  <input id="ssid" placeholder="Network name">
  <input id="pass" placeholder="Password" type="password">
  <button onclick="join()">Join network</button>
</div>

<h2>Pi software</h2>
<div class="card">
  <div class="small">Installed version</div>
  <div id="pver" class="mode">...</div>
  <div id="pinfo" class="small">Updates come from the pi folder in your GitHub repository.
    The Pi must be on home Wi-Fi.</div>
  <button class="alt" id="bCheck" onclick="checkPi()">Check for update</button>
  <button id="bInstall" style="display:none;background:#2e9e4f" onclick="installPi()">Install update</button>
</div>

<div id="msg"></div>
<p><a href="/">&larr; Back to camera</a></p>
</main>
<script>
const HS = "__HOTSPOT__";
async function status(){
  try{
    const s = await (await fetch('/api/status')).json();
    const m = document.getElementById('mode');
    m.className = 'mode ' + s.mode;
    m.textContent = s.mode==='hotspot' ? 'PiCam hotspot'
                  : s.mode==='home' ? 'Home Wi-Fi: ' + s.network : 'Not connected';
    document.getElementById('detail').textContent =
      (s.ip ? 'IP ' + s.ip : '') + (s.saved.length ? ' | Saved: ' + s.saved.join(', ') : '');
    document.getElementById('bHot').disabled  = s.mode==='hotspot';
    document.getElementById('bHome').disabled = s.mode==='home' || !s.saved.length;
  }catch(e){}
}
function say(html){
  const m = document.getElementById('msg'); m.innerHTML = html; m.style.display='block';
  m.scrollIntoView({behavior:'smooth'});
}
async function post(url, body){
  return (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify(body)})).json();
}
async function setMode(mode){
  await post('/api/mode',{mode});
  if (mode==='hotspot')
    say('Switching to hotspot. In about 20 seconds, join the <b>'+HS+'</b> Wi-Fi ' +
        'and open <b>http://10.42.0.1:8000/</b>');
  else
    say('Switching to home Wi-Fi. Rejoin your home Wi-Fi and open ' +
        '<b>http://picam.local:8000/</b> (or the Pi\\'s IP).<br><br>' +
        'If home Wi-Fi isn\\'t in range, the <b>'+HS+'</b> hotspot comes back within a minute.');
}
async function scan(){
  const box = document.getElementById('nets');
  box.textContent = 'Scanning...';
  try{
    const nets = await (await fetch('/api/scan')).json();
    if (!nets.length){
      box.textContent = 'No networks found. While on the hotspot the Pi may not be ' +
                        'able to scan - just type the network name below.';
      return;
    }
    box.innerHTML = '';
    nets.forEach(n => {
      const d = document.createElement('div'); d.className='net';
      d.innerHTML = '<span></span><span class="small">' + n.signal + '%' +
                    (n.secure ? ' &#128274;' : '') + '</span>';
      d.firstChild.textContent = n.ssid;
      d.onclick = () => { document.getElementById('ssid').value = n.ssid;
                          document.getElementById('pass').focus(); };
      box.appendChild(d);
    });
  }catch(e){ box.textContent = 'Scan failed'; }
}
async function join(){
  const ssid = document.getElementById('ssid').value.trim();
  const password = document.getElementById('pass').value;
  if (!ssid){ say('Enter a network name first'); return; }
  await post('/api/join',{ssid,password});
  say('Joining <b>' + ssid.replace(/</g,'&lt;') + '</b>. Connect your phone to that ' +
      'network and open <b>http://picam.local:8000/</b><br><br>' +
      'If the name or password was wrong, the <b>'+HS+'</b> hotspot comes back within a minute.');
}
async function checkPi(){
  const info = document.getElementById('pinfo');
  const inst = document.getElementById('bInstall');
  info.textContent = 'Checking GitHub...';
  inst.style.display = 'none';
  try{
    const r = await (await fetch('/api/update/check')).json();
    if (!r.ok){ info.textContent = r.error; return; }
    document.getElementById('pver').textContent = r.current;
    if (r.available){
      info.textContent = 'Update available: ' + r.latest;
      inst.style.display = 'block';
    } else {
      info.textContent = 'Up to date.';
    }
  }catch(e){ info.textContent = 'Check failed'; }
}
async function installPi(){
  const info = document.getElementById('pinfo');
  document.getElementById('bInstall').style.display = 'none';
  info.textContent = 'Installing...';
  try{
    const r = await post('/api/update/install', {});
    if (!r.ok){ info.textContent = 'Update failed: ' + r.error; return; }
    info.textContent = 'Installed ' + r.version + '. Restarting - this page reloads in 20 seconds.';
    setTimeout(() => location.reload(), 20000);
  }catch(e){ info.textContent = 'Update failed'; }
}
fetch('/api/version').then(r => r.json()).then(r => {
  document.getElementById('pver').textContent = r.version;
}).catch(() => {});
status(); setInterval(status, 5000);
</script>
</body></html>
"""


# ============================ Camera streaming ===============================
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(NORMAL_PAGE)
        elif path == "/vr":
            self._send(VR_PAGE)
        elif path == "/settings":
            self._send(SETTINGS_PAGE.replace("__HOTSPOT__", HOTSPOT_SSID))
        elif path == "/api/zoom":
            self._json({"zoom": zoom_level, "max": ZOOM_MAX})
        elif path == "/api/ping":
            self._send("ok", "text/plain")
        elif path == "/api/status":
            self._json(net_status())
        elif path == "/api/version":
            self._json({"version": local_version()})
        elif path == "/api/update/check":
            self._json(check_update())
        elif path == "/api/scan":
            self._json(scan_networks())
        elif path == "/stream.mjpg":
            self._stream()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "bad request"}, 400)

        if self.path == "/api/mode":
            mode = body.get("mode")
            if mode == "hotspot":
                run_later(start_hotspot)
            elif mode == "home":
                run_later(go_home)
            else:
                return self._json({"ok": False, "error": "unknown mode"}, 400)
            self._json({"ok": True})
        elif self.path == "/api/zoom":
            try:
                self._json({"zoom": set_zoom(body.get("zoom", 1)), "max": ZOOM_MAX})
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "bad zoom"}, 400)
        elif self.path == "/api/update/install":
            try:
                version = install_update()
                self._json({"ok": True, "version": version})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif self.path == "/api/join":
            ssid = str(body.get("ssid", "")).strip()
            password = str(body.get("password", ""))
            if not ssid:
                return self._json({"ok": False, "error": "no ssid"}, 400)
            run_later(join_network, ssid, password)
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            while True:
                with output.condition:
                    output.condition.wait()
                    frame = output.frame
                self.wfile.write(b"--FRAME\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except Exception as e:
            logging.info("Client %s disconnected: %s", self.client_address, e)

    def log_message(self, fmt, *args):
        pass


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser closing a page mid-load isn't an error worth printing
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    threading.Thread(target=boot_fallback, daemon=True).start()

    picam2 = Picamera2()
    camera = picam2
    config = picam2.create_video_configuration(
        main={"size": (WIDTH, HEIGHT)},
        controls={"FrameRate": FRAMERATE},
        sensor=pick_sensor_mode(picam2),
    )
    if FLIP:
        from libcamera import Transform
        config["transform"] = Transform(hflip=1, vflip=1)
    picam2.configure(config)

    output = StreamingOutput()
    picam2.start_recording(MJPEGEncoder(), FileOutput(output), quality=QUALITY)

    try:
        httpd = StreamingServer(("", PORT), StreamingHandler)
        logging.info("PiCam running on port %d", PORT)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        picam2.stop_recording()
