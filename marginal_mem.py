"""
marginal_mem.py — Motor de consolidación del precio marginal horario del MEM.

Lee los archivos diarios publicados por XM:
    iMARmmdd.txt       -> Costo marginal, Delta y MPO por hora (predespacho ideal)
    PrIdmmdd_NAL.txt   -> Generación programada por recurso en el predespacho ideal
    OFEImmdd.txt       -> Disponibilidad por unidad (D), MO, combustible ofertado (C), zonas
    capainsMMDD.txf    -> Listado de recursos (despacho central, tipo, tecnología/combustible)

y construye, para cada hora: precio, planta marginal inferida, tecnología y combustible.

Identificación de la planta marginal (sin precios de oferta):
    En el predespacho ideal, los recursos flexibles quedan en 0 o a plena disponibilidad;
    el recurso que fija el precio queda "a media carga". Candidatos por hora:
        - recurso despachado centralmente (tiene D en OFEI),
        - 0 < generación < disponibilidad efectiva (min(D, ZNOSUP)),
        - generación distinta del mínimo obligatorio (MO).
    Si hay varios candidatos se usa la consistencia del MPO: un recurso que es marginal
    en horas con el MISMO MPO suma puntos, y si aparece a media carga en horas con MPO
    distinto los pierde (típico de recursos con restricciones que no fijan precio).

Uso:
    python marginal_mem.py --carpeta ./insumos --salida ./salidas
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

HORAS = [f"H{h:02d}" for h in range(1, 25)]
TOL = 0.5  # MWh

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
ROMANOS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def sin_tildes(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", sin_tildes(s))


def norm_romano(s: str) -> str:
    """'PRADO IV' -> 'PRADO4' (para casar con la unidad PRADO4 de la OFEI)."""
    partes = re.sub(r"[^A-Z0-9 ]", " ", sin_tildes(s)).split()
    if partes and partes[-1] in ROMANOS:
        partes[-1] = str(ROMANOS[partes[-1]])
    return "".join(partes)


def leer_texto(ruta: Path) -> list[str]:
    for enc in ("utf-8", "latin-1"):
        try:
            return ruta.read_text(encoding=enc).splitlines()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"No se pudo decodificar {ruta}")


def a_float(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Lectores
# ---------------------------------------------------------------------------
def leer_imar(ruta: Path) -> pd.DataFrame:
    filas = {}
    for r in csv.reader(leer_texto(ruta)):
        if len(r) >= 25:
            filas[r[0].strip().upper()] = [a_float(x) for x in r[1:25]]
    return pd.DataFrame({
        "hora": range(1, 25),
        "costo_marginal": filas.get("COSTO MARGINAL"),
        "delta": filas.get("DELTA"),
        "mpo": filas.get("MPO"),
    })


def leer_prid(ruta: Path) -> dict[str, list[float]]:
    return {r[0].strip(): [a_float(x) for x in r[1:25]]
            for r in csv.reader(leer_texto(ruta)) if len(r) >= 25}


def leer_ofei(ruta: Path) -> tuple[dt.date | None, dict[str, dict[str, list[str]]]]:
    """Devuelve fecha y {concepto: {nombre: valores}}."""
    fecha, datos = None, defaultdict(dict)
    for line in leer_texto(ruta):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if fecha is None and m and "OFERTA" in line.upper():
            fecha = dt.date.fromisoformat(m.group(1))
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and not line.upper().startswith("AGENTE"):
            datos[p[1]][p[0]] = p[2:]
    return fecha, datos


def leer_capains(ruta: Path) -> pd.DataFrame:
    df = pd.read_csv(ruta, sep=";", encoding="latin-1", dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    df = df.drop_duplicates(subset=["PLANTA"]).copy()
    df["CAPACIDAD INSTALADA"] = pd.to_numeric(df["CAPACIDAD INSTALADA"], errors="coerce") / 1000  # kW -> MW
    return df


# ---------------------------------------------------------------------------
# Unidades OFEI -> recurso PrId
# ---------------------------------------------------------------------------
# Recursos cuyo nombre en OFEI no comparte prefijo con el recurso del despacho.
ALIAS_UNIDADES = [
    (r"^(ALTO|BAJO)ANCHICAYA\d", "ALBAN"),
    (r"^(GUADALUPE[34]\d|TRONERAS\d)", "GUATRON"),
    (r"^(PARAISO|LAGUACA)\d", "PAGUA"),
    (r"^FLORES1(GAS|VAPOR)$", "FLORES I CC"),
    (r"^FLORES[234]$", "FLORES 4 CC"),
    (r"^TEBSA\d+$", "TEBSAB CC"),
    (r"^TERMOSIERRA\d$", "TERMOSIERRA CC"),
    (r"^TERMOCENTRO\d$", "TERMOCENTRO CC"),
    (r"^TERMOEMCALI1(GAS|VAPOR)$", "TERMOEMCALI CC"),
    (r"^TERMOVALLE1(GAS|VAPOR)$", "TERMOVALLE CC"),
]


class MapeoUnidades:
    def __init__(self, recursos: list[str]):
        self.exacto = {}
        for r in recursos:
            self.exacto.setdefault(norm_romano(r), r)
        for r in recursos:  # el nombre normalizado tal cual tiene prioridad
            self.exacto[norm(r)] = r
        self.prefijos = {norm(r): r for r in recursos}

    def recurso(self, unidad: str) -> str | None:
        nu = norm(unidad)
        for rx, destino in ALIAS_UNIDADES:
            if re.search(rx, nu):
                return destino
        if nu in self.exacto:
            return self.exacto[nu]
        for i in range(len(nu) - 1, 0, -1):  # prefijo + sufijo numérico de unidad
            if nu[:i] in self.prefijos and re.fullmatch(r"\d+", nu[i:]):
                return self.prefijos[nu[:i]]
        return None


def agregar_ofei(ofei: dict, recursos: list[str]):
    mapa = MapeoUnidades(recursos)
    disp = defaultdict(lambda: [0.0] * 24)
    mo, zsup, comb = {}, {}, defaultdict(Counter)
    sin_mapa = []
    for unidad, vals in ofei.get("D", {}).items():
        rec = mapa.recurso(unidad)
        if rec is None:
            sin_mapa.append(unidad)
            continue
        for i in range(24):
            disp[rec][i] += a_float(vals[i] if i < len(vals) else 0)
    for concepto, destino in (("MO", mo), ("ZNOSUP", zsup)):
        for nombre, vals in ofei.get(concepto, {}).items():
            rec = mapa.recurso(nombre) or mapa.exacto.get(norm(nombre))
            if rec:
                destino[rec] = [a_float(v) for v in vals[:24]]
    for nombre, vals in ofei.get("C", {}).items():
        rec = mapa.recurso(nombre) or mapa.exacto.get(norm(nombre))
        if rec and vals:
            comb[rec][vals[0].upper()] += 1
    comb_dia = {k: c.most_common(1)[0][0] for k, c in comb.items()}
    return dict(disp), mo, zsup, comb_dia, sin_mapa


# ---------------------------------------------------------------------------
# Maestro de recursos (tecnología / combustible)
# ---------------------------------------------------------------------------
PALABRAS_VACIAS = {"GENERADOR", "GENERACION", "GENERA", "GEN", "FUTURA", "PLANTA", "MENOR", "PCH",
                   "CENTRAL", "SUBMERCADO", "UNIDAD", "HIDROELECTRICA", "HIDRAULICO", "TOTAL", "DE",
                   "LA", "EL", "PFV", "CICLO", "COMBINADO", "CC", "AGGE", "COGENERADOR", "S", "A"}

# Correspondencias manuales recurso del despacho -> NOMBRE en Capains (se amplía con el uso).
OVERRIDES_CAPAINS = {
    "ALBAN": "ALBAN (ALTO Y BAJO ANCHICAYA) GENERADOR",
    "PAGUA": "PARAISO GUACA GENERA",
    "GUATRON": "GUATRON GENERADOR",
    "FLORES I CC": "TERMOFLORES GENERA.",
    "FLORES 4 CC": "TERMO FLORES 4",
    "TEBSAB CC": "TEBSA TOTAL",
    "TERMOSIERRA CC": "T SIERRA1 GENERADOR",
    "TERMOCENTRO CC": "TERMOCENTRO -1",
    "TERMOEMCALI CC": "TERMOEMCALI 1",
    "TERMOVALLE CC": "TERMOVALLE1",
    "TERMOCANDELARIA CC": "TERMOCANDELARIA CICLO COMBINADO",
    "ZIPAEMG 2": "ZIPA BOGOTA 2 GEN.",
    "ZIPAEMG 3": "ZIPA BOGOTA 3 GEN.",
    "ZIPAEMG 4": "ZIPA ISA 4 GENERADOR",
    "ZIPAEMG 5": "ZIPA ISA 5 GENERADOR",
    "BARRANQUILLA 3": "TERMOBQLLA 3 GENERA.",
    "BARRANQUILLA 4": "TERMOBQLLA 4 GENERA.",
    "CARTAGENA 1": "CENTRAL CARTAGENA 1",
    "CARTAGENA 2": "UNIDAD 2 CENTRAL CARTAGENA",
    "CARTAGENA 3": "CENTRAL CARTAGENA 3",
    "GUAJIRA 1": "TERMOGUAJIRA 1",
    "GUAJIRA 2": "TERMOGUAJIRA 2",
    "PORCE II": "PORCE 2 GENERADOR",
    "PORCE III": "PORCE 3 GENERADOR",
    "MIEL I": "CENTRAL HIDROELECTRICA MIEL I",
    "SOGAMOSO": "GENERADOR HIDRAULICO SOGAMOSO",
    "ITUANGO": "GENERADOR ITUANGO",
    "EL QUIMBO": "GENERADOR EL QUIMBO",
    "CUCUANA": "GENERADOR CUCUANA",
    "SAN CARLOS": "SANCARLOS GENERADOR",
    "SAN FRANCISCO": "SANFRANCISCO GENERA.",
    "TASAJERO 1": "TASAJER 1 GENERADOR",
    "TASAJERO 2": "TASAJERO II - GENERADOR",
    "TERMONORTE": "TERMO NORTE",
    "PROELECTRICA 1": "PROELECTRICA 1 GEN.",
    "PROELECTRICA 2": "PROELECTRICA 2 GEN.",
    "TERMOCARIBE III": "FUTURA - TERMOCARIBE",
    "ATLANTICO": "Futura-Atl¿¿ntico Photovoltaic",
    "PARQUE SOLAR PUERTA DE ORO": "Futura-Parque Solar Puerta de Oro",
    "LA MATA": "FUTURA - PV LA MATA",
    "GUAYEPO": "FUTURA - GUAYEPO",
    "GUAYEPO III": "GUAYEPO 3",
    "SHANGRI LA": "PFV SHANGRI LA",
    "CARLOS LLERAS": "CARLOS LLERAS RESTREPO - GENERACION",
    "AMOYA LA ESPERANZA": "GENERACION AMOYA - LA ESPERANZA",
    "ESCUELA DE MINAS": "FUTURA - ESCUELA DE MINAS",
    "TUNJITA": "PCH TUNJITA GENERACIÓN",
    "PRADO": "PRADO GENERADOR",
    "PRADO IV": "PRADO 4 GENERADOR",
    "LA TASAJERA": "LATASAJERA GENERADOR",
}

TIPO_TXT = {"H": "Hidráulica (embalse)", "F": "Hidráulica (filo de agua)", "T": "Térmica",
            "V": "Solar fotovoltaica", "E": "Eólica"}

GRUPO_COMBUSTIBLE = {
    "AGUA": "Agua", "GASMBT": "Gas", "GAS": "Gas", "GAS NI": "Gas", "GAS IMPORTADO": "Gas",
    "CARBON": "Carbón", "MEZCLA GAS - CARBON": "Carbón", "MEZCLA GAS-CARBON": "Carbón",
    "ACPM": "Líquidos", "FOIL": "Líquidos", "COMBUSTOLEO": "Líquidos", "JET-A1": "Líquidos",
    "DIESEL MAR": "Líquidos", "QUEROSENE": "Líquidos", "CRUDO": "Líquidos", "MEZCLA GAS-FUEL": "Líquidos",
    "SOL": "Sol", "RAD SOLAR": "Sol", "VIENTO": "Viento",
    "BIOM": "Biomasa/Otros", "BIOMASA": "Biomasa/Otros", "BAGAZO": "Biomasa/Otros",
    "BIOGAS": "Biomasa/Otros", "OTROS": "Biomasa/Otros",
}


NOMBRE_COMBUSTIBLE = {"GASMBT": "Gas natural", "GAS": "Gas natural", "AGUA": "Agua", "CARBON": "Carbón",
                      "MEZCLA GAS - CARBON": "Mezcla gas-carbón", "ACPM": "ACPM", "FOIL": "Fuel oil",
                      "COMBUSTOLEO": "Combustóleo", "SOL": "Radiación solar", "VIENTO": "Viento",
                      "BIOM": "Biomasa", "OTROS": "Otros", "JET-A1": "Jet A1", "DIESEL MAR": "Diésel marino"}


def nombre_combustible(comb: str | None) -> str | None:
    if not comb:
        return None
    return NOMBRE_COMBUSTIBLE.get(sin_tildes(comb).strip(), comb.title())


def grupo_de(comb: str | None) -> str:
    if not comb:
        return "Sin clasificar"
    c = sin_tildes(comb).strip()
    if c in GRUPO_COMBUSTIBLE:
        return GRUPO_COMBUSTIBLE[c]
    if "GAS" in c and "CARBON" in c:
        return "Carbón"
    if "GAS" in c:
        return "Gas"
    return "Biomasa/Otros"


def tecnologia_de(tipo: str, comb: str, despacho: str) -> str:
    if tipo in TIPO_TXT:
        return TIPO_TXT[tipo]
    g = grupo_de(comb)
    return {"Agua": "PCH / hidráulica menor", "Sol": "Solar fotovoltaica", "Viento": "Eólica",
            "Gas": "Térmica menor / cogeneración", "Carbón": "Térmica menor / cogeneración",
            "Líquidos": "Térmica menor", "Biomasa/Otros": "Cogeneración / otros"}.get(g, "Otros")


def _clave(s: str) -> str:
    return " ".join(w for w in re.sub(r"[^A-Z0-9 ]", " ", sin_tildes(s)).split() if w not in PALABRAS_VACIAS)


def _heuristica_nombre(recurso: str) -> tuple[str, str]:
    n = sin_tildes(recurso)
    if "SOLAR" in n or n.startswith("GD ") or "PV" in n.split():
        return "O", "SOL"
    if "EOLIC" in n or "WIND" in n:
        return "O", "VIENTO"
    if any(w in n for w in ("INGENIO", "BIO", "MAYAGUEZ", "INCAUCA", "CASTILLA", "PAPELES")) or n.startswith("AUTOG"):
        return "O", "OTROS"
    if n.startswith("TERMO"):
        return "O", "GASMBT"
    return "O", "AGUA"


METODO_SIN_CAPAINS = "sin Capains (OFEI/nombre)"

# Térmicas despachadas centralmente sin campo C en la OFEI (ciclos combinados y similares)
TERMICAS_CONOCIDAS = ("TERMO", "TEBSA", "FLORES", "GUAJIRA", "PAIPA", "ZIPAEMG", "TASAJERO", "GECELCA",
                      "CARTAGENA", "BARRANQUILLA", "PROELECTRICA", "MERILECTRICA", "TESORITO", "VILLANUEVA",
                      "CANDELARIA", "DRUMMOND", "YOPAL", " CC")


def _clasificar_sin_capains(rec: str, es_dc: bool, invd: set[str], comb_dia: dict[str, str]) -> tuple[str, str]:
    """Tipo y combustible cuando no hay Capains: combustible C de la OFEI, inversores (INVD) y nombre."""
    n = sin_tildes(rec)
    if rec in comb_dia:
        return "T", comb_dia[rec]
    if rec in invd:
        return ("E" if es_dc else "O", "VIENTO") if ("EOLIC" in n or "WIND" in n) else ("V" if es_dc else "O", "SOL")
    if es_dc:
        if any(t in n or n.startswith(t) for t in TERMICAS_CONOCIDAS):
            return "T", "GASMBT"
        return "H", "AGUA"
    return _heuristica_nombre(rec)


def construir_maestro(recursos: list[str], capains: pd.DataFrame | None, recursos_dc: set[str],
                      maestro_previo: pd.DataFrame | None = None, invd: set[str] = frozenset(),
                      comb_dia: dict[str, str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Casa cada recurso del despacho con el Capains y respeta las ediciones manuales previas.

    Sin Capains, clasifica con el maestro guardado y, para recursos nuevos, con la OFEI y el nombre.
    Si hay Capains, vuelve a clasificar los recursos que antes se clasificaron sin él.
    Devuelve el maestro y la lista de recursos nuevos (no estaban en el maestro previo)."""
    comb_dia = comb_dia or {}
    previo = {}
    if maestro_previo is not None and not maestro_previo.empty:
        previo = {r["recurso"]: r for r in maestro_previo.to_dict("records")}
    nuevos = [r for r in recursos if r not in previo]

    por_nombre = {r["NOMBRE"]: r for r in capains.to_dict("records")} if capains is not None else {}
    claves = {"DC": {}, "ND": {}}
    for nombre, r in por_nombre.items():
        claves["DC" if r["DESPACHO CENTRAL"] == "DC" else "ND"].setdefault(_clave(nombre), nombre)

    filas = []
    for rec in recursos:
        if rec in previo and not (capains is not None and previo[rec]["metodo_match"] == METODO_SIN_CAPAINS):
            filas.append(previo[rec])
            continue
        es_dc = rec in recursos_dc
        nombre, metodo, score = None, "", 0.0
        if capains is None:
            tipo, comb = _clasificar_sin_capains(rec, es_dc, invd, comb_dia)
            fila = dict(recurso=rec, nombre_capains="", codigo_sic="", agente="",
                        despacho="DC" if es_dc else "ND", tipo_capains=tipo, combustible_registrado=comb,
                        cap_instalada_mw=None, metodo_match=METODO_SIN_CAPAINS, score=0.0, revisar="SI")
            fila["tecnologia"] = tecnologia_de(tipo, comb, fila["despacho"])
            fila["grupo_combustible"] = grupo_de(comb)
            filas.append(fila)
            continue
        if rec in OVERRIDES_CAPAINS and OVERRIDES_CAPAINS[rec] in por_nombre:
            nombre, metodo, score = OVERRIDES_CAPAINS[rec], "manual", 1.0
        else:
            k = _clave(rec)
            pools = [claves["DC"], claves["ND"]] if es_dc else [claves["ND"], claves["DC"]]
            for pool in pools:
                if k in pool:
                    nombre, metodo, score = pool[k], "exacto", 1.0
                    break
            if nombre is None:
                pool = {**pools[1], **pools[0]}
                cand = difflib.get_close_matches(k, list(pool), n=1, cutoff=0.8)
                if cand:
                    nombre = pool[cand[0]]
                    score = difflib.SequenceMatcher(None, k, cand[0]).ratio()
                    metodo = "aproximado"
        if nombre:
            r = por_nombre[nombre]
            tipo, comb = r["TIPO"], r["TIPO TECNOLOGIA"]
            fila = dict(recurso=rec, nombre_capains=nombre, codigo_sic=r["PLANTA"], agente=r["AGENTE"],
                        despacho="DC" if es_dc else r["DESPACHO CENTRAL"], tipo_capains=tipo,
                        combustible_registrado=comb, cap_instalada_mw=r["CAPACIDAD INSTALADA"],
                        metodo_match=metodo, score=round(score, 2),
                        revisar="SI" if metodo == "aproximado" and score < 0.9 else "")
        else:
            tipo, comb = _heuristica_nombre(rec)
            fila = dict(recurso=rec, nombre_capains="", codigo_sic="", agente="",
                        despacho="DC" if es_dc else "ND", tipo_capains=tipo, combustible_registrado=comb,
                        cap_instalada_mw=None, metodo_match="heurística por nombre", score=0.0, revisar="SI")
        fila["tecnologia"] = tecnologia_de(fila["tipo_capains"], fila["combustible_registrado"], fila["despacho"])
        fila["grupo_combustible"] = grupo_de(fila["combustible_registrado"])
        filas.append(fila)
    cols = ["recurso", "despacho", "tecnologia", "grupo_combustible", "combustible_registrado",
            "tipo_capains", "cap_instalada_mw", "nombre_capains", "codigo_sic", "agente",
            "metodo_match", "score", "revisar"]
    return pd.DataFrame(filas)[cols], nuevos


# ---------------------------------------------------------------------------
# Planta marginal
# ---------------------------------------------------------------------------
def identificar_marginal(prid, disp, mo, zsup, mpo: list[float], variables: set[str] = frozenset()):
    """variables: recursos solares/eólicos. Se consideran solo si no hay otro candidato,
    porque suelen quedar por debajo de su D por perfil de recurso y no por precio."""
    cand = []
    for h in range(24):
        c = []
        for rec, d in disp.items():
            g = prid.get(rec, [0] * 24)[h]
            tope = min(d[h], zsup[rec][h]) if rec in zsup and zsup[rec][h] > 0 else d[h]
            m = mo.get(rec, [0] * 24)[h]
            if g > TOL and g < tope - TOL and abs(g - m) > TOL:
                c.append(rec)
        firmes = [r for r in c if r not in variables]
        cand.append(firmes or c)

    clave = [round(x, 2) for x in mpo]
    resultado = []
    for h in range(24):
        if not cand[h]:
            resultado.append((None, "Sin candidato", []))
            continue
        puntajes = {}
        for rec in cand[h]:
            puntajes[rec] = sum(1 if clave[j] == clave[h] else -1 for j in range(24) if rec in cand[j])
        orden = sorted(puntajes.items(), key=lambda kv: (-kv[1], -prid[kv[0]][h]))
        ganador = orden[0][0]
        if len(cand[h]) == 1:
            conf = "Alta"
        elif len(orden) > 1 and orden[0][1] > orden[1][1]:
            conf = "Alta (desambiguada por MPO)"
        else:
            conf = "Media (empate)"
        resultado.append((ganador, conf, [r for r, _ in orden[1:]]))
    return resultado


# ---------------------------------------------------------------------------
# Proceso de un día
# ---------------------------------------------------------------------------
def buscar(carpeta: Path, patron: str) -> Path | None:
    rx = re.compile(patron, re.I)
    hits = sorted(p for p in carpeta.rglob("*") if p.is_file() and rx.search(p.name))
    return hits[-1] if hits else None


def procesar_dia(f_imar: Path, f_prid: Path, f_ofei: Path, capains: pd.DataFrame | None,
                 maestro_previo: pd.DataFrame | None = None):
    imar = leer_imar(f_imar)
    prid = leer_prid(f_prid)
    fecha, ofei = leer_ofei(f_ofei)
    recursos = list(prid)
    disp, mo, zsup, comb_dia, sin_mapa = agregar_ofei(ofei, recursos)
    mapa = MapeoUnidades(recursos)
    invd = {r for u in ofei.get("INVD", {}) if (r := mapa.recurso(u))}
    maestro, nuevos = construir_maestro(recursos, capains, set(disp), maestro_previo, invd, comb_dia)
    info = maestro.set_index("recurso")

    variables = set(maestro.loc[maestro["grupo_combustible"].isin(["Sol", "Viento"]), "recurso"])
    marg = identificar_marginal(prid, disp, mo, zsup, list(imar["mpo"]), variables)

    def comb(rec):
        return comb_dia.get(rec) or info.at[rec, "combustible_registrado"]

    horario = imar.copy()
    horario.insert(0, "fecha", fecha)
    horario["planta_marginal"] = [m[0] for m in marg]
    horario["tecnologia"] = [info.at[m[0], "tecnologia"] if m[0] else None for m in marg]
    horario["combustible"] = [nombre_combustible(comb(m[0])) if m[0] else None for m in marg]
    horario["grupo_combustible"] = [grupo_de(comb(m[0])) if m[0] else None for m in marg]
    horario["gen_marginal_mwh"] = [prid[m[0]][h] if m[0] else None for h, m in enumerate(marg)]
    horario["disp_marginal_mw"] = [disp[m[0]][h] if m[0] else None for h, m in enumerate(marg)]
    horario["confianza"] = [m[1] for m in marg]
    horario["otros_candidatos"] = [", ".join(m[2]) for m in marg]

    # Matriz de despacho (formato ancho)
    filas = []
    marg_por_rec = defaultdict(set)
    for h, m in enumerate(marg):
        if m[0]:
            marg_por_rec[m[0]].add(h + 1)
    for rec, gen in prid.items():
        fila = {"recurso": rec,
                "tecnologia": info.at[rec, "tecnologia"],
                "combustible": nombre_combustible(comb(rec)),
                "grupo_combustible": grupo_de(comb(rec)),
                "despacho": info.at[rec, "despacho"],
                "cap_instalada_mw": info.at[rec, "cap_instalada_mw"],
                "disp_max_mw": max(disp[rec]) if rec in disp else None,
                "horas_marginal": ",".join(str(h) for h in sorted(marg_por_rec.get(rec, []))),
                "gen_total_mwh": sum(gen)}
        fila.update({HORAS[i]: gen[i] for i in range(24)})
        fila.update({f"D{i+1:02d}": (disp[rec][i] if rec in disp else None) for i in range(24)})
        filas.append(fila)
    despacho = pd.DataFrame(filas)
    return fecha, horario, despacho, maestro, sin_mapa, nuevos


def elegir_capains(carpeta: Path, mmdd: str) -> Path | None:
    """Capains del mismo día; si no existe, el más reciente anterior; si no, el más reciente disponible."""
    todos = {m.group(1): p for p in carpeta.rglob("*")
             if p.is_file() and (m := re.match(r"(?i)capains(\d{4})\.tx[tf]$", p.name))}
    if not todos:
        return None
    anteriores = [k for k in todos if k <= mmdd]
    return todos[max(anteriores)] if anteriores else todos[max(todos)]


def procesar_carpeta(carpeta: Path, salida: Path, anio: int | None = None):
    """Procesa todos los días que tengan iMAR + PrId + OFEI en la carpeta."""
    from excel_marginal import exportar_dia, exportar_historico  # import diferido

    salida.mkdir(parents=True, exist_ok=True)
    cache_capains: dict[Path, pd.DataFrame] = {}

    ruta_maestro = salida / "maestro_recursos.csv"
    maestro_prev = pd.read_csv(ruta_maestro, dtype={"codigo_sic": str}) if ruta_maestro.exists() else None
    ruta_hist = salida / "historico_marginal.csv"
    hist = pd.read_csv(ruta_hist, parse_dates=["fecha"]) if ruta_hist.exists() else pd.DataFrame()

    dias = sorted({m.group(1) for p in carpeta.rglob("*") if (m := re.match(r"(?i)imar(\d{4})\.txt$", p.name))})
    procesados = []
    for mmdd in dias:
        f_imar = buscar(carpeta, rf"^imar{mmdd}\.txt$")
        f_prid = buscar(carpeta, rf"^prid{mmdd}_nal\.txt$")
        f_ofei = buscar(carpeta, rf"^ofei{mmdd}\.txt$")
        if not (f_prid and f_ofei):
            print(f"[!] {mmdd}: falta PrId u OFEI, se omite")
            continue
        habia_maestro = maestro_prev is not None
        f_cap = elegir_capains(carpeta, mmdd)
        capains = None
        if f_cap is not None:
            capains = cache_capains.setdefault(f_cap, leer_capains(f_cap))
            fuente = f"Capains {f_cap.name}"
        elif maestro_prev is not None:
            fuente = "maestro guardado (sin Capains)"
        else:
            fuente = "sin Capains ni maestro: clasificación por OFEI y nombre"
        fecha, horario, despacho, maestro, sin_mapa, nuevos = procesar_dia(f_imar, f_prid, f_ofei, capains,
                                                                           maestro_prev)
        if fecha is None:
            fecha = dt.date(anio or dt.date.today().year, int(mmdd[:2]), int(mmdd[2:]))
            horario["fecha"] = fecha
        maestro_prev = maestro
        print(f"[i] {fecha}: clasificación de recursos con {fuente}")
        despacho.to_csv(salida / f"despacho_{fecha.isoformat()}.csv", index=False, encoding="utf-8-sig")
        if habia_maestro and nuevos:
            print(f"[nuevo] {fecha}: recursos que no estaban en el maestro: {', '.join(nuevos)}")
        exportar_dia(salida / f"Despacho_Marginal_{fecha.isoformat()}.xlsx", fecha, horario, despacho, maestro,
                     fuente=fuente, nuevos=nuevos if habia_maestro else [])
        horario["fecha"] = pd.to_datetime(horario["fecha"])
        if not hist.empty:
            hist = hist[hist["fecha"] != pd.Timestamp(fecha)]
        hist = pd.concat([hist, horario], ignore_index=True)
        procesados.append(fecha)
        if sin_mapa:
            print(f"[i] {fecha}: unidades OFEI sin recurso asociado: {', '.join(sin_mapa)}")
        print(f"[OK] {fecha}: {horario['planta_marginal'].notna().sum()}/24 horas con planta marginal")

    if procesados:
        hist = hist.sort_values(["fecha", "hora"]).reset_index(drop=True)
        hist.to_csv(ruta_hist, index=False, encoding="utf-8-sig")
        maestro_prev.to_csv(ruta_maestro, index=False, encoding="utf-8-sig")
        exportar_historico(salida / "Historico_Marginal.xlsx", hist)
    return procesados


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Consolida precio y planta marginal horaria (XM)")
    ap.add_argument("--carpeta", type=Path, default=Path("insumos"))
    ap.add_argument("--salida", type=Path, default=Path("salidas"))
    ap.add_argument("--anio", type=int, default=None, help="Año si la OFEI no trae fecha")
    a = ap.parse_args()
    procesar_carpeta(a.carpeta, a.salida, a.anio)
