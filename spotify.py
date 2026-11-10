"""
Webserver: fragt den Standort im Browser ab, setzt ihn per GPS Joystick am
Android-Gerät, öffnet Spotify -> Profil -> Abo -> Family verwalten und zeigt
am Ende einen Screenshot. Der Fortschritt wird live auf der Seite angezeigt.

Funktioniert über USB und WLAN (adb).

Start:   python adb_server.py
Aufruf:  http://localhost:8000

Nur Python-Standardbibliothek (Python 3.8+).
"""

import base64
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --- Konfiguration -----------------------------------------------------------

ADB = "adb"                      # oder voller Pfad, z.B. r"C:\platform-tools\adb.exe"
# Bevorzugtes Gerät (USB-Seriennummer oder "IP:Port"). None = automatisch:
# ist nur ein Gerät verbunden (USB oder WLAN), wird es genommen, sonst
# wählst du es auf der Webseite aus.
DEVICE = None
HOST, PORT = "127.0.0.1", 8000

# Ablauf: (Beschreibung, adb-Argumente, Pause danach in Sekunden)
# {lat} und {lng} werden durch die Browser-Koordinaten ersetzt.
STEPS = [
    ("Display an",       ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], 1),
    ("Entsperren",       ["shell", "wm", "dismiss-keyguard"], 1),
    ("Standort setzen",
     ["shell", "am", "start-foreground-service", "-a", "theappninjas.gpsjoystick.TELEPORT",
      "--ef", "lat", "{lat}", "--ef", "lng", "{lng}"], 3),
    ("Spotify starten",
     ["shell", "monkey", "-p", "com.spotify.music",
      "-c", "android.intent.category.LAUNCHER", "1"], 6),
    ("Profil-Icon",      ["shell", "input", "tap", "85", "141"], 3),
    ("Abo",              ["shell", "input", "tap", "354", "527"], 4),
    ("Family verwalten", ["shell", "input", "tap", "905", "726"], 5),
]

# --- Job-Status (wird von /status an den Browser geliefert) ------------------

lock = threading.Lock()          # schützt JOB
JOB = {"running": False}


def job_update(**kw):
    with lock:
        JOB.update(kw)


def job_log(line):
    with lock:
        JOB["log"].append(line)


def job_step(index, state):
    with lock:
        JOB["steps"][index]["state"] = state   # pending | running | waiting | done | error

# --- adb ---------------------------------------------------------------------

def adb(args, device=None, timeout=30):
    cmd = [ADB] + (["-s", device] if device else []) + args
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def list_devices():
    """Liefert [{'serial': ..., 'type': 'USB'|'WLAN', 'model': ...}] aller bereiten Geräte."""
    r = adb(["devices", "-l"], timeout=10)
    devices = []
    for line in r.stdout.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":   # 'unauthorized', 'offline' überspringen
            continue
        serial = parts[0]
        model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), "")
        wlan = ":" in serial or "._adb-tls-connect." in serial
        devices.append({"serial": serial, "type": "WLAN" if wlan else "USB", "model": model})
    return devices


def pick_device(requested=None):
    serials = [d["serial"] for d in list_devices()]
    for wanted in (requested, DEVICE):
        if wanted:
            if wanted in serials:
                return wanted
            raise RuntimeError(f"Gerät {wanted} nicht verbunden. Verfügbar: {serials or 'keine'}")
    if len(serials) == 1:
        return serials[0]
    if not serials:
        raise RuntimeError("Kein Gerät verbunden (USB-Debugging erlaubt? 'adb devices' prüfen)")
    raise RuntimeError(f"Mehrere Geräte verbunden, bitte eins auswählen: {serials}")


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def screenshot_png(device):
    # 1. Versuch: direkt über exec-out. Manche Geräte schreiben Warnungen
    #    (z.B. "Multiple displays were found") vor das Bild -> PNG-Anfang suchen.
    r = adb(["exec-out", "screencap", "-p"], device)
    pos = r.stdout.find(PNG_SIG)
    if pos >= 0:
        return r.stdout[pos:]
    first = (r.stdout[:200] + r.stderr[:200]).decode(errors="replace").strip()

    # 2. Versuch: Screenshot auf dem Handy speichern und per pull holen
    tmp_remote = "/data/local/tmp/adb_server_shot.png"
    adb(["shell", "screencap", "-p", tmp_remote], device)
    r2 = adb(["exec-out", "cat", tmp_remote], device)
    adb(["shell", "rm", "-f", tmp_remote], device)
    pos = r2.stdout.find(PNG_SIG)
    if pos >= 0:
        return r2.stdout[pos:]

    raise RuntimeError(
        f"screencap fehlgeschlagen (Code {r.returncode}, {len(r.stdout)} Bytes): "
        f"{first or 'keine Ausgabe'}")

# --- Ablauf (läuft im Hintergrund-Thread) ------------------------------------

def run_flow(device, lat, lng):
    try:
        job_log(f"Gerät: {device}")
        adb(["shell", "svc", "power", "stayon", "true"], device)
        job_log("Display bleibt an (svc power stayon true)")

        for i, (name, args, pause) in enumerate(STEPS):
            args = [a.format(lat=lat, lng=lng) for a in args]
            job_step(i, "running")
            job_update(current=name, wait_until=None)
            r = adb(args, device)
            out = (r.stdout + r.stderr).decode(errors="replace").strip()
            job_log(f"[{name}] adb {' '.join(args)}" + (f"\n  {out}" if out else ""))
            if r.returncode != 0:
                job_step(i, "error")
                raise RuntimeError(f"Schritt '{name}' fehlgeschlagen (Code {r.returncode})")
            job_step(i, "waiting")
            job_update(wait_until=time.time() + pause)
            time.sleep(pause)
            job_step(i, "done")

        job_update(current="Screenshot", wait_until=None)
        job_log("Screenshot wird erstellt...")
    except Exception as e:
        job_update(error=str(e))
        job_log(f"FEHLER: {e}")
    finally:
        # Screenshot auch bei Fehler, damit man sieht, wo es hängt
        try:
            img = base64.b64encode(screenshot_png(device)).decode()
            job_update(image=img)
        except Exception as e:
            job_log(f"Screenshot fehlgeschlagen: {e}")
        adb(["shell", "svc", "power", "stayon", "false"], device)
        job_log("Display-Timeout wieder normal (svc power stayon false)")
        job_update(running=False, current=None, wait_until=None, finished=time.time())

# --- HTML --------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>ADB Family</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body { font-family: sans-serif; background:#111; color:#eee; margin:16px; max-width:900px; }
 button { padding:10px 18px; font-size:16px; background:#1db954; color:#fff;
          border:0; border-radius:6px; cursor:pointer; }
 button:disabled { background:#555; cursor:wait; }
 #status { margin:12px 0; font-weight:bold; }
 ul#steps { list-style:none; padding:0; margin:8px 0; }
 #steps li { padding:6px 10px; margin:3px 0; background:#1c1c1c; border-radius:4px; }
 #steps li .icon { display:inline-block; width:1.5em; }
 #steps li.running, #steps li.waiting { background:#1d3a2a; }
 #steps li.done  { color:#8c8; }
 #steps li.error { background:#4a1d1d; color:#f88; }
 #steps li.pending { color:#777; }
 .bar { height:6px; background:#333; border-radius:3px; overflow:hidden; margin:8px 0; }
 .bar > div { height:100%; width:0; background:#1db954; transition:width .3s; }
 pre { background:#222; padding:8px; white-space:pre-wrap; font-size:13px; max-height:220px; overflow:auto; }
 #shot { max-height:80vh; max-width:100%; border:1px solid #444; margin-top:8px; }
 #mapbox { margin:12px 0; }
 #map { height:300px; border-radius:6px; border:1px solid #444; }
 #maplinks { font-size:14px; margin-top:4px; }
 #maplinks a { color:#8cf; margin-right:12px; }
 .leaflet-container img { margin:0 !important; border:0 !important;
                          max-width:none !important; max-height:none !important; }
 #maperr { color:#f88; font-size:13px; }
</style></head><body>
<h2>ADB Family</h2>
<p>Gerät: <select id="dev"></select> <a href="#" id="reload" style="color:#8cf">neu laden</a></p>
<button id="locate">1. Standort abfragen</button>
<button id="go" disabled>2. Befehle starten</button>
<p style="font-size:13px;color:#aaa">Nach der Abfrage kannst du den Marker auf der Karte verschieben
oder in die Karte klicken, um den Standort zu korrigieren.</p>
<div id="status"></div>
<div id="mapbox" hidden>
  <div id="map"></div>
  <div id="maplinks"></div>
  <div id="maperr"></div>
</div>
<div class="bar"><div id="prog"></div></div>
<ul id="steps"></ul>
<pre id="log" hidden></pre>
<img id="shot" hidden alt="Screenshot">
<script>
const $ = id => document.getElementById(id);
const btn = $('go'), st = $('status'), log = $('log'), img = $('shot'), dev = $('dev');
const ICON = { pending:'○', running:'⏳', waiting:'⏱', done:'✔', error:'✖' };
let timer = null;
let map = null, marker = null, circle = null;
let chosen = null;            // {lat, lng} – wird an /run geschickt
const locBtn = $('locate');

// Karte mit OpenStreetMap (kein Token nötig)
function ensureMap() {
  $('mapbox').hidden = false;
  if (map) { map.invalidateSize(); return; }
  if (typeof L === 'undefined') {
    $('maperr').textContent = 'Kartenbibliothek (Leaflet) konnte nicht geladen werden – Internet / Adblocker prüfen.';
    return;
  }
  map = L.map('map', { center: [47.5, 13.5], zoom: 7 });   // Startansicht Österreich
  let tileErrors = 0;
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).on('tileerror', () => {
    if (++tileErrors === 3)
      $('maperr').textContent = 'Kartenkacheln werden nicht geladen (Internet, Firewall oder Adblocker?).';
  }).on('tileload', () => { tileErrors = 0; $('maperr').textContent = ''; })
    .addTo(map);
  // Klick in die Karte = Standort manuell setzen
  map.on('click', e => setChosen(e.latlng.lat, e.latlng.lng, null, 'manuell'));
}

function setChosen(lat, lng, accuracy, source, recenter) {
  ensureMap();
  chosen = { lat: (+lat).toFixed(6), lng: (+lng).toFixed(6) };
  if (!map) { if (!timer) btn.disabled = false; return; }
  if (!marker) {
    marker = L.marker([lat, lng], { draggable: true }).addTo(map);
    marker.on('dragend', () => {
      const p = marker.getLatLng();
      setChosen(p.lat, p.lng, null, 'manuell');
    });
  } else {
    marker.setLatLng([lat, lng]);
  }
  if (circle) { circle.remove(); circle = null; }
  if (accuracy) circle = L.circle([lat, lng], { radius: accuracy, weight: 1 }).addTo(map);
  if (recenter) {
    map.invalidateSize();
    if (circle) map.fitBounds(circle.getBounds(), { maxZoom: 17 });
    else map.setView([lat, lng], 16);
  }
  const { lat: la, lng: ln } = chosen;
  const acc = accuracy ? `Genauigkeit ca. ${Math.round(accuracy)} m` : 'manuell gesetzt';
  $('maplinks').innerHTML =
    `<b>${la}, ${ln}</b> (${source}, ${acc}) · ` +
    `<a href="https://www.google.com/maps?q=${la},${ln}" target="_blank">Google Maps</a>` +
    `<a href="https://www.bing.com/maps?cp=${la}~${ln}&lvl=17&sp=point.${la}_${ln}" target="_blank">Bing Maps</a>` +
    `<a href="https://www.openstreetmap.org/?mlat=${la}&mlon=${ln}#map=17/${la}/${ln}" target="_blank">OpenStreetMap</a>`;
  if (!timer) btn.disabled = false;
}

// Standort mehrere Sekunden lang verfeinern und den genauesten Wert nehmen
function locate() {
  if (!navigator.geolocation) { st.textContent = 'Geolocation wird nicht unterstützt.'; return; }
  locBtn.disabled = true;
  let best = null;
  const started = Date.now(), MAX_MS = 12000, GOOD_M = 30;
  st.textContent = 'Standort wird abgefragt...';
  let ended = false;
  const finish = () => {
    if (ended) return;
    ended = true;
    navigator.geolocation.clearWatch(watch);
    clearInterval(tick);
    locBtn.disabled = false;
    if (!best) { st.textContent = 'Kein Standort erhalten – bitte in die Karte klicken.'; ensureMap(); return; }
    st.textContent = best.coords.accuracy > 200
      ? `Standort ungenau (±${Math.round(best.coords.accuracy)} m) – bitte Marker verschieben.`
      : `Standort ermittelt (±${Math.round(best.coords.accuracy)} m).`;
  };
  const watch = navigator.geolocation.watchPosition(pos => {
    if (!best || pos.coords.accuracy < best.coords.accuracy) {
      best = pos;
      setChosen(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, 'Browser', true);
    }
    if (pos.coords.accuracy <= GOOD_M) finish();
  }, err => {
    st.textContent = 'Standort nicht verfügbar: ' + err.message;
    finish();
  }, { enableHighAccuracy: true, maximumAge: 0, timeout: MAX_MS });
  const tick = setInterval(() => {
    const left = Math.max(0, (MAX_MS - (Date.now() - started)) / 1000);
    if (best) st.textContent = `Verfeinere Standort... aktuell ±${Math.round(best.coords.accuracy)} m (noch ${left.toFixed(0)} s)`;
    if (left <= 0) finish();
  }, 500);
}
locBtn.onclick = locate;

async function loadDevices() {
  dev.innerHTML = '';
  try {
    const list = await (await fetch('/devices')).json();
    if (!list.length) dev.innerHTML = '<option value="">– kein Gerät –</option>';
    for (const d of list) {
      const o = document.createElement('option');
      o.value = d.serial;
      o.textContent = `${d.type}: ${d.model || d.serial} (${d.serial})`;
      dev.appendChild(o);
    }
  } catch (e) { dev.innerHTML = '<option value="">Fehler beim Laden</option>'; }
}
$('reload').onclick = e => { e.preventDefault(); loadDevices(); };
loadDevices();

function render(j) {
  const steps = j.steps || [];
  $('steps').innerHTML = steps.map(s =>
    `<li class="${s.state}"><span class="icon">${ICON[s.state] || ''}</span>${s.name}</li>`).join('');
  const done = steps.filter(s => s.state === 'done').length;
  $('prog').style.width = steps.length ? (100 * done / steps.length) + '%' : '0';
  if (j.log && j.log.length) { log.hidden = false; log.textContent = j.log.join('\\n'); log.scrollTop = log.scrollHeight; }

  if (j.running) {
    let txt = j.current ? `Läuft: ${j.current}` : 'Läuft...';
    if (j.wait_until) {
      const left = Math.max(0, j.wait_until - j.now);
      txt += ` – warte ${left.toFixed(1)} s`;
    }
    st.textContent = txt;
  } else if (j.finished) {
    st.textContent = j.error ? 'Fehler: ' + j.error : 'Fertig.';
    if (j.image) { img.src = 'data:image/png;base64,' + j.image; img.hidden = false; }
    stopPolling();
  }
}

async function poll() {
  try { render(await (await fetch('/status')).json()); }
  catch (e) { st.textContent = 'Verbindung zum Server verloren: ' + e; stopPolling(); }
}
function startPolling() { stopPolling(); timer = setInterval(poll, 400); poll(); }
function stopPolling()  {
  if (timer) clearInterval(timer);
  timer = null;
  btn.disabled = !chosen; locBtn.disabled = false;
}

btn.onclick = async () => {
  if (!chosen) return;
  btn.disabled = true; locBtn.disabled = true;
  img.hidden = true; log.hidden = true; $('steps').innerHTML = '';
  st.textContent = `Starte mit ${chosen.lat}, ${chosen.lng}...`;
  try {
    const r = await fetch(`/run?lat=${chosen.lat}&lng=${chosen.lng}&device=${encodeURIComponent(dev.value)}`);
    const d = await r.json();
    if (!r.ok) { st.textContent = 'Fehler: ' + d.error; stopPolling(); return; }
    startPolling();
  } catch (e) { st.textContent = 'Fehler: ' + e; stopPolling(); }
};

// Läuft beim Laden der Seite schon ein Ablauf? Dann direkt mitverfolgen.
fetch('/status').then(r => r.json()).then(j => { if (j.running) { btn.disabled = true; startPolling(); } });
</script>
</body></html>"""

# --- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # /status-Polling nicht in der Konsole spammen
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)

        if url.path == "/":
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")

        elif url.path == "/devices":
            try:
                self.send_json(200, list_devices())
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif url.path == "/status":
            with lock:
                data = json.loads(json.dumps(JOB))   # Kopie
            data["now"] = time.time()
            self.send_json(200, data)

        elif url.path == "/screen.png":
            try:
                device = pick_device(q.get("device", [None])[0] or None)
                self.send(200, screenshot_png(device), "image/png")
            except Exception as e:
                self.send(500, str(e).encode(), "text/plain; charset=utf-8")

        elif url.path == "/run":
            try:
                lat = float(q["lat"][0]); lng = float(q["lng"][0])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValueError
            except (KeyError, ValueError):
                self.send_json(400, {"error": "Ungültige Koordinaten"})
                return
            try:
                device = pick_device(q.get("device", [None])[0] or None)
            except Exception as e:
                self.send_json(400, {"error": str(e)})
                return

            with lock:
                if JOB.get("running"):
                    self.send_json(409, {"error": "Es läuft bereits ein Ablauf"})
                    return
                JOB.clear()
                JOB.update(running=True, current="Start", wait_until=None, error=None,
                           image=None, finished=None, log=[],
                           steps=[{"name": n, "state": "pending"} for n, _, _ in STEPS])

            threading.Thread(target=run_flow,
                             args=(device, f"{lat:.6f}", f"{lng:.6f}"),
                             daemon=True).start()
            self.send_json(202, {"started": True})

        else:
            self.send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"Server läuft: http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()