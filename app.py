import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from geopy.geocoders import ArcGIS
import folium
from folium.plugins import MarkerCluster
from datetime import datetime, timedelta

# --- NASTAVENIA ---
MESTO = "Bratislava"
MAX_STRANOK = 6         # Prehľadávame prvých x strán
MAX_DNI_STARE = 7      # Zobrazíme byty z posledných 7 dní
MAX_CENA = 700          # 💰 MAXIMÁLNA POVOLENÁ CENA (v EUR)
DB_FILE = 'databaza_bytov.json'

geolocator = ArcGIS(timeout=10)

MESTSKE_CASTI = [
    "Staré Mesto", "Ružinov", "Petržalka", "Nové Mesto", "Karlova Ves", "Dúbravka",
    "Rača", "Vrakuňa", "Podunajské Biskupice", "Devínska Nová Ves", "Lamač",
    "Záhorská Bystrica", "Vajnory", "Jarovce", "Rusovce", "Čunovo", "Devín",
    "Trnávka", "Kramáre", "Dlhé diely", "Dlhé Diely", "Koliba", "Pošeň", "Ostredky", 
    "Prievoz", "Mlynská dolina", "Nivy", "Štrkovec", "Bory", "Slnečnice"
]

STOP_WORDS_ULICA = {
    'bratislava', 'prenájom', 'prenajom', 'byt', '2-izbový', '2 izbový', '1-izbový', '1 izbový',
    'poschodie', 'centrum', 'novostavba', 'ponuke', 'popis', 'popis bytu',
    'dispozícia', 'dispozicia', 'rekonštrukcia', 'rekonstrukcia', 'cena',
    'bratislava i', 'bratislava ii', 'bratislava iii', 'bratislava iv', 'bratislava v',
    'mail', 'email', 'e-mail', 'fotografie', 'fotky', 'foto', 'zašleme', 'zasleme', 
    'pošleme', 'posleme', 'vyžiadanie', 'vyziadanie', 'dohodu', 'kontakt', 'telefon', 
    'telefón', 'píšte', 'piste', 'volať', 'volat', 'info', 'správa', 'sprava', 'obhliadka',
    'na', 'za', 'do', 'v', 's', 'z', 'o'
}


# --- POMOCNÉ FUNKCIE ---

DISCORD_WEBHOOK_URL = os.environ.get("JOZO")

def posli_discord_notifikaciu(titulok, cena, lokalita, odkaz):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ Notifikácia neodišla: DISCORD_WEBHOOK_URL nie je načítaný v prostredí!")
        return
    
    # Discord Embed správne formátuje správy do peknej karty
    payload = {
        "username": "Bazoš Bytový Bot",
        "avatar_url": "https://reality.bazos.sk/favicon.ico",
        "embeds": [
            {
                "title": f"🚨 {titulok}",
                "url": odkaz,
                "color": 3447003,  # Modrá farba
                "fields": [
                    {
                        "name": "💰 Cena",
                        "value": str(cena),
                        "inline": True
                    },
                    {
                        "name": "📍 Lokalita",
                        "value": str(lokalita),
                        "inline": True
                    }
                ],
                "footer": {
                    "text": "Bazoš Monitor"
                }
            }
        ]
    }
    
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Chyba pri posielaní Discord notifikácie: {e}")

def parsuj_datum(text):
    match = re.search(r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})', text)
    if match:
        den, mesiac, rok = map(int, match.groups())
        return datetime(rok, mesiac, den)
    
    match_bez_roku = re.search(r'(\d{1,2})\.\s*(\d{1,2})\.', text)
    if match_bez_roku:
        den, mesiac = map(int, match_bez_roku.groups())
        return datetime(datetime.now().year, mesiac, den)
        
    return None


def extrahuj_psc_z_html(soup_detail):
    if not soup_detail:
        return None
    text_page = soup_detail.get_text()
    match = re.search(r'Lokalita:\s*(\d{3}\s?\d{2})', text_page, re.IGNORECASE)
    if match:
        psc_raw = match.group(1).replace(' ', '')
        return f"{psc_raw[:3]} {psc_raw[3:]}"
        
    match_ba = re.search(r'\b(8[1-5]\d\s?\d{2})\b', text_page)
    if match_ba:
        psc_raw = match_ba.group(1).replace(' ', '')
        return f"{psc_raw[:3]} {psc_raw[3:]}"

    return None


def najdi_mestsku_cast(text):
    for mc in MESTSKE_CASTI:
        pattern = r'\b(?:Bratislava\s*[\-–\s]\s*)?' + re.escape(mc) + r'\b'
        if re.search(pattern, text, re.IGNORECASE):
            return mc
    return None


def je_validny_nazov_ulice(kandidat):
    if not kandidat or len(kandidat) < 3:
        return False
    slova = re.findall(r'\b[a-zA-ZáčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9]+\b', kandidat.lower())
    for s in slova:
        if s in STOP_WORDS_ULICA:
            return False
    if re.match(r'^(?:bratislava|ba)\s*(?:i|ii|iii|iv|v|[1-5])?$', kandidat.lower()):
        return False
    return True


def ziskaj_kandidatov_ulic(text):
    if not text:
        return []
    candidates = []
    patterns = [
        r'\b([A-ZÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9][a-zA-ZáčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9\.\-\/]{1,25}(?:\s*(?:[\/\&\-]\s*|\s+)[A-ZÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9][a-zA-ZáčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9\.\-\/]{1,25}){0,3})\s+(?:ulica|ul\.|ul|námestie|namestie|nám\.|nám)\b',
        r'\b(?:ulica|ul\.|ul|námestie|namestie|nám\.|nám)\b\s*[:\-–,]*\s*([A-ZÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9][a-zA-ZáčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9\.\-\/]{1,25}(?:\s*(?:[\/\&\-]\s*|\s+)[A-ZÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9][a-zA-ZáčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓÔŔŠŤÚÝŽ0-9\.\-\/]{1,25}){0,3})'
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start_idx = max(0, match.start() - 25)
            pre_text = text[start_idx:match.start()].lower()
            if any(w in pre_text for w in ['blízko', 'blizko', 'v blízkosti', 'v blizkosti', 'nedaleko', 'neďaleko', 'okolo']):
                continue
            ulica = match.group(1).strip()
            ulica = re.split(r'[,;\n\r\(\)<>|]', ulica)[0].strip().rstrip('.')
            if je_validny_nazov_ulice(ulica):
                if ulica not in candidates:
                    candidates.append(ulica)
    return candidates


def extrahuj_cislo(text):
    if not text:
        return None
    text_clean = re.sub(r'(\d{3,4}),-', r'\1', text)
    text_clean = re.sub(r'\b(\d{1,2})[\s\.](\d{3})\b', r'\1\2', text_clean)
    match = re.search(r'(\d{3,4})', text_clean)
    return int(match.group(1)) if match else None


def extrahuj_spolu_cenu(text):
    if not text:
        return None

    text_clean = text.replace('\xa0', ' ').replace('&nbsp;', ' ')
    text_clean = re.sub(r'(\d{3,4}),-', r'\1', text_clean)
    text_clean = re.sub(r'\b(\d{1,2})[\s\.](\d{3})\b', r'\1\2', text_clean)

    match_plus = re.search(r'\b(\d{3,4})\s*(?:€|eur)?\s*\+\s*(\d{2,3})\s*(?:€|eur)?', text_clean, re.IGNORECASE)
    if match_plus:
        najom = int(match_plus.group(1))
        energie = int(match_plus.group(2))
        if 200 <= najom <= 2000 and 30 <= energie <= 500:
            return najom + energie

    vzory = [
        r'\b(?:spolu|celkom|komplet|celková cena|celkova cena|cena spolu|celkový nájom|celkovy najom)\b[^\d]{0,20}?(\d{3,4})\b',
        r'\b(\d{3,4})\s*(?:€|eur)?\s*(?:spolu|celkom|komplet)\b',
        r'\b(\d{3,4})\s*(?:€|eur)?\s*(?:s\s+energiami|vrátane\s+energií|vrátene\s+energií|vrátane\s+e\b|s\s+e\b|vrátane\s+en\b)',
        r'\b(?:s\s+energiami|vrátane\s+energií|vrátane\s+e|s\s+e)\b[^\d]{0,20}?(\d{3,4})\b'
    ]

    for vzor in vzory:
        match = re.search(vzor, text_clean, re.IGNORECASE)
        if match:
            hodnota = int(match.group(1))
            if 250 <= hodnota <= 2500:
                return hodnota

    return None


def vyries_lokalitu_a_gps(titulok, plny_popis, psc, geolocator, mesto="Bratislava"):
    cely_text = f"{titulok} {plny_popis}"
    najdena_mc = najdi_mestsku_cast(cely_text)

    # 1. Titulok
    for ulica in ziskaj_kandidatov_ulic(titulok):
        adresa = f"{ulica}, {najdena_mc if najdena_mc else mesto}"
        try:
            loc = geolocator.geocode(adresa)
            if loc:
                return ulica, f"{ulica}" + (f" ({najdena_mc})" if najdena_mc else ""), loc.latitude, loc.longitude, "green"
        except Exception:
            pass

    # 2. Popis
    for ulica in ziskaj_kandidatov_ulic(plny_popis):
        adresa = f"{ulica}, {najdena_mc if najdena_mc else mesto}"
        try:
            loc = geolocator.geocode(adresa)
            if loc:
                return ulica, f"{ulica}" + (f" ({najdena_mc})" if najdena_mc else ""), loc.latitude, loc.longitude, "green"
        except Exception:
            pass

    # 3. PSČ
    if psc:
        try:
            loc = geolocator.geocode(f"{psc} Bratislava, Slovakia")
            if loc:
                return None, f"PSČ: {psc}" + (f" ({najdena_mc})" if najdena_mc else ""), loc.latitude, loc.longitude, "orange"
        except Exception:
            pass

    # 4. Mestská časť
    if najdena_mc:
        try:
            loc = geolocator.geocode(f"Bratislava - {najdena_mc}")
            if loc:
                return None, f"Mestská časť: {najdena_mc}", loc.latitude, loc.longitude, "blue"
        except Exception:
            pass

    # 5. Fallback
    try:
        loc = geolocator.geocode(mesto)
        if loc:
            return None, "Neuvedená (Bratislava)", loc.latitude, loc.longitude, "gray"
    except Exception:
        pass

    return None, "Neuvedená", 48.1486, 17.1077, "gray"


# --- HLAVNÝ SKRIPT ---

hranicny_datum = datetime.now() - timedelta(days=MAX_DNI_STARE)
print(f"📅 Načítavam byty pridané po: {hranicny_datum.strftime('%d.%m.%Y')}")

if os.path.exists(DB_FILE):
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        databaza_bytov = json.load(f)
else:
    databaza_bytov = {}

nove_pribudli = 0
odfiltrovane_drahé = 0

for strana in range(MAX_STRANOK):
    offset = strana * 20
    url = f"https://reality.bazos.sk/prenajmu/byt/{'' if offset==0 else str(offset)+'/'}?hledat=bratislava&hlokalita=&humkreis=10&cenaod=&cenado=700&order="
    
    print(f"--- Sťahujem stranu {strana + 1} ---")
    response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
    soup = BeautifulSoup(response.text, 'html.parser')

    for inzerat in soup.find_all('div', class_='inzeraty'):
        link_elem = inzerat.find('h2', class_='nadpis').find('a')
        titulok = link_elem.text.strip()
        odkaz = "https://reality.bazos.sk" + link_elem['href']
        cena_bazos_raw = inzerat.find('div', class_='inzeratycena').text.strip()
        inzerat_id = odkaz.split('/')[-2]

        dt_inzeratu = parsuj_datum(inzerat.text)
        if dt_inzeratu and dt_inzeratu < hranicny_datum:
            continue

        datum_str = dt_inzeratu.strftime("%d.%m.%Y") if dt_inzeratu else "Neznámy"
        datum_iso = dt_inzeratu.strftime("%Y-%m-%d") if dt_inzeratu else "2000-01-01"

        try:
            time.sleep(0.2)
            res_detail = requests.get(odkaz, headers={'User-Agent': 'Mozilla/5.0'})
            soup_detail = BeautifulSoup(res_detail.text, 'html.parser')
            popis_elem = soup_detail.find('div', class_='popis') or soup_detail.find('div', class_='popisdetail') or soup_detail.find('div', class_='kompletpopis')
            plny_popis = popis_elem.text.strip() if popis_elem else ""
            najdene_psc = extrahuj_psc_z_html(soup_detail)
        except Exception:
            plny_popis = ""
            najdene_psc = None

        for odsek in ["Podobné inzeráty", "Inzeráty používateľa", "Odpovedať na inzerát"]:
            if odsek in plny_popis:
                plny_popis = plny_popis.split(odsek)[0]

        cely_text_detail = f"{titulok} {plny_popis}"
        
        bazos_cena_num = extrahuj_cislo(cena_bazos_raw)
        spolu_cena_num = extrahuj_spolu_cenu(cely_text_detail)

        if spolu_cena_num:
            efektivna_cena = spolu_cena_num
        elif bazos_cena_num and bazos_cena_num >= 150:
            efektivna_cena = bazos_cena_num
        else:
            efektivna_cena = None

        # Pevný filter drahých bytov
        if efektivna_cena and efektivna_cena > MAX_CENA:
            odfiltrovane_drahé += 1
            if inzerat_id in databaza_bytov:
                del databaza_bytov[inzerat_id]
            print(f"  💸 [VYRADENÝ {efektivna_cena}€ > {MAX_CENA}€]: {titulok[:35]}...")
            continue

        ulica_str, lokalita_zobraz, lat, lng, farba = vyries_lokalitu_a_gps(titulok, plny_popis, najdene_psc, geolocator, MESTO)

        v_db = inzerat_id in databaza_bytov
        databaza_bytov[inzerat_id] = {
            "titulok": titulok,
            "odkaz": odkaz,
            "cena_bazos": cena_bazos_raw,
            "spolu_cena": f"{spolu_cena_num} €" if spolu_cena_num else None,
            "efektivna_cena": efektivna_cena,
            "datum_str": datum_str,
            "datum_iso": datum_iso,
            "lokalita": lokalita_zobraz,
            "lat": lat,
            "lng": lng,
            "farba": farba
        }
        
        if not v_db:
            nove_pribudli += 1
        spolu_info = f" (Spolu: {spolu_cena_num}€)" if spolu_cena_num else ""
        print(f"  ✨ [PRIDANÝ BYT {efektivna_cena}€{spolu_info}] Lokalita: '{lokalita_zobraz}' | {titulok[:30]}...")
        posli_discord_notifikaciu(titulok, efektivna_cena, lokalita_zobraz, odkaz)

# 🧹 STRUKTÚRNE ČISTENIE DATABÁZY (Odstráni staré aj drahé položky)
aktualizovana_db = {}
for b_id, b_data in databaza_bytov.items():
    dt = datetime.strptime(b_data["datum_iso"], "%Y-%m-%d")
    ef_cena = b_data.get("efektivna_cena")
    
    # Podmienka: Musí byť nový a cena nesmie presahovať MAX_CENA
    if dt >= hranicny_datum and (ef_cena is None or ef_cena <= MAX_CENA):
        aktualizovana_db[b_id] = b_data

with open(DB_FILE, 'w', encoding='utf-8') as f:
    json.dump(aktualizovana_db, f, ensure_ascii=False, indent=2)


# --- VYTVORENIE MAPY ---
mapa = folium.Map(location=[48.1486, 17.1077], zoom_start=13)
marker_cluster = MarkerCluster(spiderfyOnMaxZoom=True).add_to(mapa)

for b_id, b in aktualizovana_db.items():
    spolu_riadok = f"<b>💰 Cena Spolu:</b> <b style='color:#d9534f; font-size:1.1em;'>{b['spolu_cena']}</b><br>" if b['spolu_cena'] else ""
    
    popup_text = f"""
    <div style="width: 270px; font-family: Arial, sans-serif; font-size: 13px; line-height: 1.5;">
        <h4 style="margin: 0 0 8px 0; color: #2c3e50; font-size: 14px; border-bottom: 1px solid #eee; padding-bottom: 5px;">
            {b['titulok']}
        </h4>
        <b>Cena Bazoš:</b> {b['cena_bazos']}<br>
        {spolu_riadok}
        <b>📅 Dátum:</b> {b['datum_str']}<br>
        <b>📍 Lokalita:</b> {b['lokalita']}<br><br>
        <a href='{b['odkaz']}' target='_blank' style='background-color: #337ab7; color: white; text-decoration: none; padding: 6px 12px; border-radius: 4px; display: inline-block; font-weight: bold; width: 88%; text-align: center;'>
            Otvoriť na Bazoši 🔗
        </a>
        <br><br>
        <button id="btn-skryt-{b_id}" onclick="toggleSkryt('{b_id}')" style="background-color: #e74c3c; color: white; border: none; padding: 7px 12px; border-radius: 4px; cursor: pointer; width: 100%; font-weight: bold; transition: 0.2s;">
            👻 Stmaviť / Priesvitný byt
        </button>
    </div>
    """

    folium.Marker(
        location=[b['lat'], b['lng']],
        popup=folium.Popup(popup_text, max_width=320),
        icon=folium.Icon(color=b['farba'], icon="home"),
        options={'inzeratId': str(b_id)}  # 🎯 Priamy kľúč pre JavaScript
    ).add_to(marker_cluster)


# --- SPROSTREDKOVANÝ JAVASCRIPT PRE SKUTOČNÉ SPRIESVITNENIE IKONY ---
skryt_script = """
<script>
function ziskajIdInzeratu(m) {
    if (m && m.options && m.options.inzeratId) {
        return String(m.options.inzeratId);
    }
    if (!m || !m.getPopup || !m.getPopup()) return null;
    let content = m.getPopup().getContent();
    let htmlStr = (typeof content === 'string') ? content : (content.innerHTML || content.outerHTML || '');
    let match = htmlStr.match(/toggleSkryt\(['"]([^'"]+)['"]\)/);
    return match ? match[1] : null;
}

function aktualizujVzhladMarkera(m) {
    let id = ziskajIdInzeratu(m);
    if (!id) return;
    
    let skryte = JSON.parse(localStorage.getItem('skryte_byty') || '[]');
    let jeSkryty = skryte.includes(String(id));

    // Natívne nastaví priesvitnosť 20%
    if (m.setOpacity) {
        m.setOpacity(jeSkryty ? 0.2 : 1.0);
    }

    // Aplikuje sivý filter priamo na DOM prvok
    let el = m.getElement ? m.getElement() : m._icon;
    if (el) {
        if (jeSkryty) {
            el.style.filter = 'grayscale(100%) opacity(25%)';
            el.style.webkitFilter = 'grayscale(100%) opacity(25%)';
        } else {
            el.style.filter = 'none';
            el.style.webkitFilter = 'none';
        }
    }
}

function toggleSkryt(id) {
    id = String(id);
    let skryte = JSON.parse(localStorage.getItem('skryte_byty') || '[]');
    let idx = skryte.indexOf(id);
    
    if (idx > -1) {
        skryte.splice(idx, 1);
    } else {
        skryte.push(id);
    }
    
    localStorage.setItem('skryte_byty', JSON.stringify(skryte));

    let btn = document.getElementById('btn-skryt-' + id);
    let jeSkryty = skryte.includes(id);
    if (btn) {
        if (jeSkryty) {
            btn.innerText = '👁️ Obnoviť / Zobraziť byt';
            btn.style.backgroundColor = '#27ae60';
        } else {
            btn.innerText = '👻 Stmaviť / Priesvitný byt';
            btn.style.backgroundColor = '#e74c3c';
        }
    }

    obnovVsetkyMarkery();
}

function obnovVsetkyMarkery() {
    let mapaObj = null;
    for (let key in window) {
        if (window[key] instanceof L.Map) {
            mapaObj = window[key];
            break;
        }
    }
    if (!mapaObj) return;

    mapaObj.eachLayer(function(layer) {
        if (layer instanceof L.MarkerClusterGroup) {
            layer.getLayers().forEach(aktualizujVzhladMarkera);
        } else if (layer instanceof L.Marker) {
            aktualizujVzhladMarkera(layer);
        }
    });
}

// Háčik priamo na vykresľovanie ikon v Leaflete
if (typeof L !== 'undefined' && L.Marker) {
    let origOnAdd = L.Marker.prototype.onAdd;
    L.Marker.prototype.onAdd = function(map) {
        origOnAdd.apply(this, arguments);
        let self = this;
        setTimeout(function() {
            aktualizujVzhladMarkera(self);
        }, 10);
    };
}

window.addEventListener('load', function() {
    setTimeout(obnovVsetkyMarkery, 500);
});
</script>
"""
mapa.get_root().html.add_child(folium.Element(skryt_script))

mapa.save("index.html")

print(f"\n🎉 HOTOVO!")
print(f"  + Pribudlo {nove_pribudli} nových bytov.")
print(f"  🗺️ Na mape sa celkovo zobrazuje {len(aktualizovana_db)} bytov.")
